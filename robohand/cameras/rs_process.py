"""A RealSense camera (D405 / D435) running in its own process.

At full resolution one camera's capture, depth alignment, colorizing and encoder feeding cost about a CPU core
of Python-held work; several of them in the dashboard process starve each other on the GIL (5 cameras reached
~15 fps). Each camera therefore runs in a worker process that

* captures and aligns at the full profile (the in-process ``Camera`` class, unchanged),
* records its own colour / depth tracks with the same ``TrackWriter`` the dashboard used, on the shared
  host clock (``time.monotonic`` is system-wide on Windows), and
* sends the dashboard a 640 px preview at a low rate (1 fps while recording).

``ProcessCamera`` is the dashboard-side handle. It has the ``Camera`` interface the hub, the D435 group and
the recorder already use (``start/stop/snapshot/set_range/running/thread/stop_event/events``), plus
``full_packet`` (one full-resolution frame, for snapshots and calibration checks) and the recording calls.
"""
from __future__ import annotations

from dataclasses import replace
import itertools
import logging
import multiprocessing
import queue
import threading
import time

import cv2

PREVIEW_WIDTH = 640
PREVIEW_FPS = 10


def preview_of(packet, width=PREVIEW_WIDTH):
    """Downscaled copy of a ``Frame``: colour and depth palette by area, raw depth by sample decimation
    (depth values are never interpolated)."""
    height, full_width = packet.depth.shape[:2]
    step = max(1, full_width // width)
    if step == 1:
        return packet
    size = (full_width // step, height // step)
    small = replace(packet, color=cv2.resize(packet.color, size, interpolation=cv2.INTER_AREA),
                    depth=packet.depth[::step, ::step][:size[1], :size[0]].copy(), palette=None)
    small.depth_rgb   # colorize the small depth here, not in the dashboard
    return small


def _stream_frames(camera, slot, packet=None, at=None):
    """``StreamFrame``s of one camera packet (default: the newest), keyed like the dashboard streams
    (``<slot>_color`` / ``<slot>_depth``); live when the camera runs and the packet is within 2 s of `at`."""
    from .hub import StreamFrame
    packet = packet or camera.snapshot()[0]
    if packet is None:
        return {}
    at = time.monotonic() if at is None else at
    live = camera.running and not camera.stop_event.is_set() and abs(at - packet.received_at) < 2
    return {f"{slot}_color": StreamFrame(packet.color, False, packet.number, packet.received_at,
                                         packet.captured_at, packet.fps, live),
            f"{slot}_depth": StreamFrame(packet.depth, False, packet.number, packet.received_at,
                                         packet.captured_at, packet.fps, live, packet.depth,
                                         (packet.near, packet.far))}


class _Recording:
    """Worker side of one recording: TrackWriters fed on the output timeline by a driver thread."""

    def __init__(self, camera, folder, keys, fps, report):
        from pathlib import Path
        from .hub import StreamFrame
        from .recorder import TrackWriter
        self.camera, self.fps = camera, fps
        self.slot = keys[0].rsplit("_", 1)[0]
        frames = _stream_frames(camera, self.slot)
        missing = [key for key in keys if key not in frames or not frames[key].live]
        if missing:
            raise RuntimeError("실시간 영상이 없습니다: " + ", ".join(missing))
        self.StreamFrame = StreamFrame
        self.tracks = {key: TrackWriter(key, frames[key], Path(folder), time.monotonic(), fps,
                                        lambda key, error: report(("track_error", key, error)))
                       for key in keys}
        for track in self.tracks.values():
            track.start_worker()
        for track in self.tracks.values():
            if not track.ready.wait(10):
                raise RuntimeError("녹화 인코더 준비 시간이 초과됐습니다.")
            if track.error:
                raise RuntimeError(track.error)
        self.stop = threading.Event()
        self.thread = None
        self.last = -1

    def begin(self, start):
        self.start = start
        for track in self.tracks.values():
            track.start = start
        self.thread = threading.Thread(target=self._drive, daemon=True, name="Record-driver")
        self.thread.start()

    # Output frame i shows the packet captured nearest to start + i/fps. Choosing from the camera's short
    # history LAG seconds after that moment makes the choice independent of when this thread wakes up
    # (picking "the newest packet at wake-up" turned scheduling jitter into repeated + skipped frames).
    LAG = .1

    def _emit(self, index):
        target = self.start + index / self.fps
        candidates = [packet for packet in self.camera.recent() if packet.number >= self.used_number]
        moment = lambda packet: packet.capture_time or packet.received_at
        packet = min(candidates, key=lambda packet: abs(moment(packet) - target)) if candidates else None
        current = _stream_frames(self.camera, self.slot, packet, target) if packet else {}
        if packet:
            self.used_number = packet.number
        for key, track in self.tracks.items():
            frame = current.get(key)
            if frame is None:
                old = track.initial
                frame = self.StreamFrame(old.image, old.bgr, old.number, old.received_at,
                                         old.captured_at, 0, False)
            track.submit(index, frame)
        self.last = index

    def _drive(self):
        fps, start = self.fps, self.start
        self.used_number = -1
        while not self.stop.is_set():
            due = int((time.monotonic() - self.LAG - start) * fps)
            while self.last < due:
                self._emit(self.last + 1)
            self.stop.wait(max(.001, (self.last + 1) / fps + self.LAG - (time.monotonic() - start)))

    def end(self, count):
        self.stop.set()
        if self.thread:
            self.thread.join(5)
            while self.last < count - 1:     # the last LAG seconds: choose from what has arrived
                self._emit(self.last + 1)
        for track in self.tracks.values():
            track.finish(count)
        for track in self.tracks.values():
            track.thread.join()
        return {key: {"stats": track.stats, "error": track.error, "missing": track.missing,
                      "frames_written": track.frames_written}
                for key, track in self.tracks.items()}

    def abort(self):
        try:
            self.end(max(1, self.last + 1))
        except Exception:
            logging.exception("Recording abort failed")


def _worker(serial, model, profile, near, far, usb2_fps, commands, events, previews):
    """Child process: capture loop, command handling, preview publishing."""
    from ..paths import configure_logging
    from .d405 import Camera
    configure_logging("realsense_worker")
    cv2.setNumThreads(2)
    try:   # capture first: above-normal priority (the encoders this process starts run below normal)
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x8000)
    except (AttributeError, OSError):
        pass
    camera = Camera(serial, model=model, **profile)
    camera.set_range(near, far)
    camera.usb2_fps = usb2_fps
    camera.start()
    preview_interval, last_preview, last_number = 1 / PREVIEW_FPS, 0.0, None
    recording = None
    report = events.put
    try:
        while True:
            while not camera.events.empty():
                report(camera.events.get())
            try:
                command = commands.get(timeout=.01)
            except queue.Empty:
                command = None
            if command:
                kind, token, *arguments = command
                try:
                    if kind == "stop":
                        break
                    if kind == "range":
                        camera.set_range(*arguments)
                    elif kind == "preview_fps":
                        preview_interval = 1 / arguments[0]
                    elif kind == "full_packet":
                        report(("reply", token, camera.snapshot()[0], None))
                    elif kind == "prepare":
                        if recording:
                            recording.abort()
                        recording = None
                        recording = _Recording(camera, *arguments, report)
                        report(("reply", token, True, None))
                    elif kind == "begin":
                        recording.begin(*arguments)
                        report(("reply", token, True, None))
                    elif kind == "end":
                        result = recording.end(*arguments) if recording else {}
                        recording = None
                        report(("reply", token, result, None))
                except Exception as error:
                    logging.exception("RealSense worker command %s failed", kind)
                    if token is not None:
                        report(("reply", token, None, str(error)))
            packet = camera.snapshot()[0]
            now = time.monotonic()
            if packet is not None and packet.number != last_number and now - last_preview >= preview_interval:
                last_preview, last_number = now, packet.number
                small = preview_of(packet)
                try:
                    previews.put_nowait(small)
                except queue.Full:
                    try:
                        previews.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        previews.put_nowait(small)
                    except queue.Full:
                        pass
            if not camera.running and camera.events.empty():
                break
    except Exception as error:
        logging.exception("RealSense worker failed")
        report(("error", str(error)))
    finally:
        if recording:
            recording.abort()
        camera.stop()
        if camera.thread:
            camera.thread.join(5)
        report(("stopped", None))


class ProcessCamera:
    """Dashboard handle of a RealSense camera whose capture and recording run in a worker process."""

    def __init__(self, serial=None, resolution=(640, 480), fps=None, model="D405", color_resolution=None,
                 usb2_resolution=None):
        self.serial, self.model = serial, model
        self.profile = {"resolution": resolution, "fps": fps, "color_resolution": color_resolution,
                        "usb2_resolution": usb2_resolution}
        self.near, self.far = 0.07, 0.5
        self.usb2_fps = 30
        self.lock = threading.Lock()
        self.events = queue.SimpleQueue()
        self.thread = None            # pump thread; ends after the worker process has exited
        self.stop_event = threading.Event()
        self.process = None
        self.latest = None
        self.info = {}
        self.replies = {}
        self.tokens = itertools.count()
        self.on_track_error = None
        self.commands = None

    @property
    def running(self):
        return (self.process is not None and self.process.is_alive()) or (
            self.thread is not None and self.thread.is_alive())

    def start(self):
        if self.running:
            return
        self.stop_event.clear()
        with self.lock:
            self.latest = None
        context = multiprocessing.get_context("spawn")
        self.commands, self.worker_events, self.previews = context.Queue(), context.Queue(), context.Queue(maxsize=2)
        self.process = context.Process(
            target=_worker, daemon=True, name=f"{self.model}-{self.serial}",
            args=(self.serial, self.model, self.profile, self.near, self.far, self.usb2_fps,
                  self.commands, self.worker_events, self.previews))
        self.process.start()
        self.thread = threading.Thread(target=self._pump, daemon=True, name=f"{self.model}-{self.serial}-pump")
        self.thread.start()

    def _pump(self):
        stopped = False
        while True:
            try:
                packet = self.previews.get(timeout=.05)
                with self.lock:
                    self.latest = packet
            except queue.Empty:
                pass
            except (EOFError, OSError):
                break
            while True:
                try:
                    event = self.worker_events.get_nowait()
                except queue.Empty:
                    break
                except (EOFError, OSError):
                    event = None
                    break
                kind = event[0]
                if kind == "reply":
                    _, token, value, error = event
                    waiter = self.replies.get(token)
                    if waiter:
                        waiter[1].update(value=value, error=error)
                        waiter[0].set()
                elif kind == "track_error":
                    if self.on_track_error:
                        self.on_track_error(event[1], event[2])
                else:
                    if kind == "info":
                        with self.lock:
                            self.info = event[1]
                    if kind == "stopped":
                        stopped = True
                    self.events.put(event)
            if not self.process.is_alive() and self.worker_events.empty():
                break
        if not stopped:
            self.events.put(("stopped", None))
        for event, result in list(self.replies.values()):   # nobody is left to answer
            result.setdefault("error", "카메라 프로세스가 종료됐습니다.")
            event.set()

    def stop(self):
        self.stop_event.set()
        if self.process is not None and self.process.is_alive():
            self.commands.put(("stop", None))

    def snapshot(self):
        with self.lock:
            return self.latest, dict(self.info)

    def set_range(self, near, far):
        if not 0.01 <= near < far <= 10:
            raise ValueError("10 ≤ 가까운 거리 < 먼 거리 ≤ 10000 mm로 입력하세요.")
        with self.lock:
            self.near, self.far = near, far
        if self.running:
            self.commands.put(("range", None, near, far))

    def set_preview_fps(self, fps):
        if self.running:
            self.commands.put(("preview_fps", None, fps))

    def request(self, kind, *arguments, timeout=10):
        if not self.running:
            raise RuntimeError(f"{self.model} {self.serial}: 카메라가 실행 중이 아닙니다.")
        token = next(self.tokens)
        waiter = (threading.Event(), {})
        self.replies[token] = waiter
        try:
            self.commands.put((kind, token, *arguments))
            if not waiter[0].wait(timeout):
                raise RuntimeError(f"{self.model} {self.serial}: 응답이 없습니다 ({kind}).")
            if waiter[1].get("error"):
                raise RuntimeError(f"{self.model} {self.serial}: {waiter[1]['error']}")
            return waiter[1].get("value")
        finally:
            self.replies.pop(token, None)

    def full_packet(self, timeout=5):
        """The newest full-resolution ``Frame`` (snapshots, calibration checks)."""
        return self.request("full_packet", timeout=timeout)

    def prepare_recording(self, folder, keys, fps, on_error=None):
        """Start this camera's encoders for `keys` (its ``<slot>_color`` / ``<slot>_depth`` streams)."""
        self.on_track_error = on_error
        self.request("prepare", str(folder), list(keys), fps, timeout=20)

    def begin_recording(self, start):
        self.request("begin", start)

    def end_recording(self, count):
        """Pad the tracks to `count` frames, close them; {key: {"stats", "error", "missing", ...}}."""
        try:
            return self.request("end", count, timeout=120)
        finally:
            self.on_track_error = None
