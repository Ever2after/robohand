"""Cameras beside the three D405s: RealSense D435 RGB-D and 4K UVC webcams (e.g. OBSBOT).

A camera runs while its group is on screen or while it is selected for recording, so the
extra USB streams and decoders cost nothing when they are not used.

Webcams are read with PyAV through DirectShow as the camera's own MJPEG packets:
* dashboard: a reduced JPEG decode (4K -> 960 px) at PREVIEW_FPS,
* snapshot: the original JPEG of the latest frame, untouched,
* recording: packets are stream-copied into MKV (no re-encode, full camera frame rate) with
  a per-frame CSV of host-arrival and device timestamps.
Media Foundation is avoided: four 4K MJPEG streams opened through MSMF in the Tk process
stalled Tk timers and redraws for 10-40 s at a time.
"""
from __future__ import annotations

from collections import deque
import csv
from datetime import datetime
from fractions import Fraction
import json
import logging
from pathlib import Path
import queue
import threading
import time

import av
import cv2
import numpy as np
from PIL import Image
import pyrealsense2 as rs

from ..paths import CAPTURES
from .rs_process import ProcessCamera as RealSenseCamera   # capture + recording in a worker process
from .hub import StreamFrame, new_folder

D435_SLOTS = ("d435_1", "d435_2")
WEBCAM_SLOTS = ("rgb4k_1", "rgb4k_2", "rgb4k_3", "rgb4k_4")
GROUPS = {
    "d435": {"title": "D435 RGB-D", "slots": D435_SLOTS,
             "streams": [f"{slot}_{kind}" for slot in D435_SLOTS for kind in ("color", "depth")]},
    "webcam": {"title": "4K RGB", "slots": WEBCAM_SLOTS,
               "streams": [f"{slot}_color" for slot in WEBCAM_SLOTS]},
}
STREAM_TITLES = {
    **{f"d435_{n}_{kind}": f"D435 {n} {caption}" for n in (1, 2)
       for kind, caption in (("color", "컬러"), ("depth", "깊이"))},
    **{f"rgb4k_{n}_color": f"4K RGB {n}" for n in (1, 2, 3, 4)},
}
D435_RANGE_M = (.2, 3.0)
# Full sensor resolution on USB 3: native 1280x720 depth aligned to the 1920x1080 colour image (30 fps).
# USB 2 offers neither profile, so the camera falls back to 640x480 (15 fps via usb2_fps).
D435_PROFILE = {"resolution": (1280, 720), "color_resolution": (1920, 1080), "fps": None,
                "usb2_resolution": (640, 480)}
WEBCAM_SIZES = {"4K": (3840, 2160), "1080p": (1920, 1080), "720p": (1280, 720)}
WEBCAM_FPS = 30
PREVIEW_WIDTH = 960
PREVIEW_FPS = 10
RECORDING_PREVIEW_FPS = 1   # dashboard redraw / webcam preview decode rate while recording
MKV_TIME_BASE = Fraction(1, 1000)


def group_of(slot):
    return next(group for group, spec in GROUPS.items() if slot in spec["slots"])


def list_webcams():
    """Physical USB cameras that are not RealSense nodes, in a stable (device path) order.
    Software cameras (e.g. "OBSBOT Virtual Camera") have no USB vendor id and are skipped."""
    from cv2_enumerate_cameras import enumerate_cameras
    cameras = [camera for camera in enumerate_cameras(cv2.CAP_DSHOW)
               if camera.vid and camera.vid != 0x8086 and camera.path.lower().startswith("\\\\?\\usb")
               and "realsense" not in camera.name.lower() and "virtual" not in camera.name.lower()]
    return sorted(cameras, key=lambda camera: camera.path.lower())


def reduced_decode(jpeg, width):
    """Decode a JPEG at roughly PREVIEW_WIDTH using libjpeg's 1/2, 1/4, 1/8 scaling."""
    flags = {1: cv2.IMREAD_COLOR, 2: cv2.IMREAD_REDUCED_COLOR_2, 4: cv2.IMREAD_REDUCED_COLOR_4,
             8: cv2.IMREAD_REDUCED_COLOR_8}
    factor = max((f for f in flags if width / f >= PREVIEW_WIDTH), default=1)
    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), flags[factor])
    if image is not None and image.shape[1] > PREVIEW_WIDTH * 1.2:
        height = round(image.shape[0] * PREVIEW_WIDTH / image.shape[1])
        image = cv2.resize(image, (PREVIEW_WIDTH, height), interpolation=cv2.INTER_AREA)
    return image


class WebcamTake:
    """One recording of one webcam: MJPEG packets copied into MKV + timestamps CSV.
    Only the capture thread touches it."""

    def __init__(self, folder, slot, started_at, template):
        self.folder, self.slot, self.started_at = Path(folder), slot, started_at
        self.path = self.folder / f"{slot}.mkv"
        self.container = av.open(str(self.path), "w", format="matroska")
        self.stream = self.container.add_stream_from_template(template)
        self.stream.time_base = MKV_TIME_BASE
        self.csv_file = (self.folder / f"{slot}_timestamps.csv").open("w", newline="", encoding="utf-8")
        self.rows = csv.writer(self.csv_file)
        self.rows.writerow(["frame", "t_host_s", "t_device_s", "bytes"])
        self.frames = self.bytes = 0
        self.last_pts = -1
        self.first_t = self.last_t = None
        self.device_lag = []

    def write(self, packet, host, device_s):
        t_host = host - self.started_at
        if t_host < 0:
            return  # captured before the session clock started
        pts = max(int(round(t_host * 1000)), self.last_pts + 1)
        size = packet.size
        packet.stream = self.stream
        packet.time_base = MKV_TIME_BASE
        packet.pts = packet.dts = pts
        packet.duration = None
        self.container.mux(packet)
        t_device = device_s - self.started_at if device_s is not None else None
        self.rows.writerow([self.frames, f"{t_host:.6f}", "" if t_device is None else f"{t_device:.6f}", size])
        if t_device is not None and len(self.device_lag) < 5000:
            self.device_lag.append(t_host - t_device)
        self.last_pts = pts
        self.frames += 1
        self.bytes += size
        self.first_t = t_host if self.first_t is None else self.first_t
        self.last_t = t_host

    def close(self):
        try:
            self.container.close()
        finally:
            self.csv_file.close()
        duration = (self.last_t - self.first_t) if self.frames > 1 else 0
        lag = sorted(self.device_lag)
        return {"file": self.path.name, "timestamps": f"{self.slot}_timestamps.csv", "codec": "MJPEG (camera stream copy)",
                "frames": self.frames, "bytes": self.path.stat().st_size if self.path.exists() else 0,
                "first_t_host_s": self.first_t, "last_t_host_s": self.last_t,
                "mean_fps": (self.frames - 1) / duration if duration > 0 else None,
                "host_minus_device_ms_median": lag[len(lag) // 2] * 1000 if lag else None}


class Webcam:
    """One UVC camera (MJPEG over DirectShow) captured on its own thread with PyAV."""

    def __init__(self, path, name, size=WEBCAM_SIZES["4K"]):
        self.path, self.name, self.size = path, name, tuple(size)
        self.lock = threading.Lock()
        self.events = queue.SimpleQueue()
        self.thread = None
        self.stop_event = threading.Event()
        self.preview = None        # PREVIEW_WIDTH-wide StreamFrame (dashboard)
        self.latest_jpeg = None    # (bytes, frame number, host time) of the newest frame
        self.info = {"name": name, "path": path}
        self.take = None
        self.commands = queue.SimpleQueue()   # recording start/stop, executed by the capture thread
        self.preview_fps = PREVIEW_FPS        # dashboard decode rate; lowered while recording

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        if self.running:
            return
        self.stop_event.clear()
        with self.lock:
            self.preview = self.latest_jpeg = None
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"Webcam-{self.path[-12:]}")
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def snapshot(self):
        with self.lock:
            return self.preview, dict(self.info)

    def preview_frame(self):
        with self.lock:
            return self.preview

    def jpeg(self):
        with self.lock:
            return self.latest_jpeg

    def begin_recording(self, folder, slot, started_at, timeout=5):
        done = threading.Event()
        result = {}
        self.commands.put(("begin", (folder, slot, started_at), done, result))
        if not done.wait(timeout):
            raise RuntimeError(f"{self.name}: 녹화를 시작하지 못했습니다 (응답 없음).")
        if "error" in result:
            raise RuntimeError(f"{self.name}: {result['error']}")

    def end_recording(self, timeout=10):
        done = threading.Event()
        result = {}
        self.commands.put(("end", None, done, result))
        if not self.running or not done.wait(timeout):
            return {"error": "카메라가 녹화 종료 전에 멈췄습니다."}
        return result.get("summary") or {"error": result.get("error", "녹화 요약 없음")}

    def _handle_commands(self, stream):
        while not self.commands.empty():
            kind, argument, done, result = self.commands.get()
            try:
                if kind == "begin":
                    if self.take:
                        self.take.close()
                    folder, slot, started_at = argument
                    self.take = WebcamTake(folder, slot, started_at, stream)
                elif self.take:
                    result["summary"] = self.take.close()
                    self.take = None
            except Exception as error:
                logging.exception("Webcam recording command failed")
                result["error"] = str(error)
                self.take = None
            finally:
                done.set()

    def _run(self):
        container = None
        try:
            container = av.open(f"video=@device_pnp_{self.path}", format="dshow", timeout=10, options={
                "video_size": f"{self.size[0]}x{self.size[1]}", "framerate": str(WEBCAM_FPS),
                "vcodec": "mjpeg", "rtbufsize": "256M"})
            stream = container.streams.video[0]
            width, height = stream.codec_context.width, stream.codec_context.height
            with self.lock:
                self.info.update(resolution=[width, height], codec="mjpeg", requested_fps=WEBCAM_FPS)
            self.events.put(("info", dict(self.info)))
            times, number, last_preview = deque(maxlen=60), 0, 0.0
            for packet in container.demux(stream):
                if self.stop_event.is_set():
                    break
                self._handle_commands(stream)
                if packet.size == 0:
                    continue
                host = time.monotonic()
                # DirectShow stamps samples with the system reference clock, the same QPC
                # timeline as time.monotonic() on Windows.
                device_s = float(packet.pts * packet.time_base) if packet.pts is not None else None
                jpeg = bytes(packet)
                number += 1
                times.append(host)
                fps = (len(times)-1) / (times[-1]-times[0]) if len(times) > 1 else 0.0
                if self.take:
                    try:
                        self.take.write(packet, host, device_s)
                    except Exception as error:
                        logging.exception("Webcam recording write failed")
                        self.events.put(("error", f"녹화 기록 실패: {error}"))
                        self.take = None
                preview = None
                if host - last_preview >= 1 / self.preview_fps:
                    last_preview = host
                    small = reduced_decode(jpeg, width)
                    if small is not None:
                        preview = StreamFrame(small, True, number, host, datetime.now().astimezone().isoformat(),
                                              fps, True, source_size=(width, height))
                with self.lock:
                    self.latest_jpeg = (jpeg, number, host)
                    if preview is not None:
                        self.preview = preview
                    elif self.preview is not None:   # keep the shown fps current between previews
                        self.preview = StreamFrame(self.preview.image, True, self.preview.number,
                                                   self.preview.received_at, self.preview.captured_at, fps, True,
                                                   source_size=(width, height))
        except Exception as error:
            logging.exception("Webcam error")
            if not self.stop_event.is_set():
                message = str(error)
                if "I/O error" in message or "Could not" in message or "busy" in message.lower():
                    message += " · OBSBOT Center 등 다른 프로그램이 카메라를 쓰고 있으면 종료하세요."
                self.events.put(("error", f"{self.name}: {message}"))
        finally:
            if self.take:
                try:
                    self.take.close()
                except Exception:
                    logging.exception("Webcam recording close failed")
                self.take = None
            # Release anyone waiting on a recording command.
            while not self.commands.empty():
                _, _, done, result = self.commands.get()
                result["error"] = "카메라가 멈췄습니다."
                done.set()
            if container is not None:
                container.close()
            self.events.put(("stopped", None))


class WebcamRecordingSource:
    """Recorder source (status / begin / end) for the selected webcams of one session."""

    def __init__(self, extras, slots):
        self.extras, self.slots = extras, list(slots)
        self.active = []

    def status(self):
        cameras = self.extras.cameras()
        frames = self.extras.frames()
        missing = [STREAM_TITLES[f"{slot}_color"] for slot in self.slots
                   if not isinstance(cameras.get(slot), Webcam)
                   or not (frames.get(f"{slot}_color") and frames[f"{slot}_color"].live)]
        return {"live": not missing, "message": ("영상 없음: " + ", ".join(missing)) if missing else ""}

    def begin(self, folder, started_at):
        cameras = self.extras.cameras()
        for slot in self.slots:
            cameras[slot].begin_recording(folder, slot, started_at)
            self.active.append((slot, cameras[slot]))

    def end(self):
        if not self.active:
            return None
        cameras = {}
        for slot, camera in self.active:
            cameras[slot] = camera.end_recording() | {"device": camera.snapshot()[1]}
        self.active = []
        errors = [f"{slot}: {summary['error']}" for slot, summary in cameras.items() if summary.get("error")]
        return {"records": sum(summary.get("frames", 0) for summary in cameras.values()),
                "cameras": cameras, "error": "; ".join(errors) or None,
                "timing": "t_host_s = packet arrival on time.monotonic() - start_monotonic_s; "
                          "t_device_s = DirectShow sample time on the same clock; MKV pts = t_host_s in ms"}


class ExtraCameras:
    """Discovers and runs the D435 / webcam groups that are wanted (shown or selected for recording)."""

    def __init__(self):
        self.lock = threading.RLock()
        self.devices = {}          # slot -> RealSenseCamera | Webcam
        self.wanted = frozenset()  # slots that should run
        self.events = queue.SimpleQueue()
        self.switching = threading.Lock()
        self.generation = 0
        self.webcam_size = WEBCAM_SIZES["4K"]
        self.preview_fps = PREVIEW_FPS

    def set_preview_fps(self, fps):
        """Dashboard preview decode rate of the webcams (recording is unaffected)."""
        self.preview_fps = fps
        for camera in self.cameras().values():
            if isinstance(camera, Webcam):
                camera.preview_fps = fps
            else:
                camera.set_preview_fps(fps)

    def set_webcam_size(self, size):
        """Capture resolution of the webcams; restarts the running ones."""
        size = tuple(size)
        if size == self.webcam_size:
            return
        self.webcam_size = size
        if self.wanted & set(WEBCAM_SLOTS):
            self.activate(self.wanted)   # _discover replaces webcams whose size differs

    def cameras(self):
        with self.lock:
            return dict(self.devices)

    @property
    def running(self):
        return any(camera.running for camera in self.cameras().values())

    def activate(self, slots):
        """Run exactly `slots` (any of D435_SLOTS / WEBCAM_SLOTS); stop the others."""
        with self.lock:
            self.wanted = frozenset(slots)
            self.generation += 1
            generation = self.generation
        threading.Thread(target=self._switch, args=(self.wanted, generation), daemon=True,
                         name="ExtraCameras").start()

    def stop_all(self):
        self.activate(())

    def _switch(self, wanted, generation):
        with self.switching:  # one switch at a time; a newer request supersedes this one
            if generation != self.generation:
                return
            for slot, camera in self.cameras().items():
                if slot not in wanted:
                    camera.stop()
            for slot, camera in self.cameras().items():
                if slot not in wanted and camera.thread:
                    camera.thread.join(5)
                    with self.lock:
                        if self.devices.get(slot) is camera:
                            del self.devices[slot]
            for group, spec in GROUPS.items():
                slots = [slot for slot in spec["slots"] if slot in wanted]
                if not slots:
                    continue
                try:
                    found = self._discover(group)
                except Exception as error:
                    logging.exception("Extra camera discovery failed")
                    self.events.put(("error", f"{spec['title']} 검색 실패: {error}"))
                    continue
                for slot in slots:
                    if generation != self.generation:
                        return
                    camera = found.get(slot)
                    with self.lock:
                        previous = self.devices.get(slot)
                        if camera is None:
                            self.devices.pop(slot, None)
                        else:
                            self.devices[slot] = camera
                    if previous is not None and previous is not camera:
                        previous.stop()
                        if previous.thread:
                            previous.thread.join(5)
                    if camera is not None and not camera.running:
                        camera.start()
                        time.sleep(.15)  # stagger USB stream negotiation
                self.events.put(("devices", group, {slot: found[slot].snapshot()[1] |
                                                    {"serial": getattr(found[slot], "serial", None)}
                                                    for slot in slots if slot in found}))

    def _discover(self, group):
        current = self.cameras()
        found = {}
        if group == "d435":
            serials = sorted(device.get_info(rs.camera_info.serial_number)
                             for device in rs.context().query_devices()
                             if "D435" in device.get_info(rs.camera_info.name))
            for slot, serial in zip(D435_SLOTS, serials):
                previous = current.get(slot)
                if isinstance(previous, RealSenseCamera) and previous.serial == serial:
                    found[slot] = previous
                    continue
                camera = RealSenseCamera(serial, model="D435", **D435_PROFILE)
                camera.set_range(*D435_RANGE_M)
                camera.usb2_fps = 15
                found[slot] = camera
        else:
            for slot, info in zip(WEBCAM_SLOTS, list_webcams()):
                previous = current.get(slot)
                if isinstance(previous, Webcam) and previous.path == info.path and previous.size == self.webcam_size:
                    found[slot] = previous
                else:
                    found[slot] = Webcam(info.path, info.name, self.webcam_size)
                    found[slot].preview_fps = self.preview_fps
        return found

    def frames(self):
        """StreamFrames of the running cameras, keyed like the preview panels (webcams: preview copy)."""
        now = time.monotonic()
        result = {}
        for slot, camera in self.cameras().items():
            if slot not in self.wanted:
                continue
            packet = camera.preview_frame() if isinstance(camera, Webcam) else camera.snapshot()[0]
            if packet is None:
                continue
            live = camera.running and not camera.stop_event.is_set() and now-packet.received_at < 2
            if isinstance(camera, Webcam):
                result[f"{slot}_color"] = StreamFrame(packet.image, True, packet.number, packet.received_at,
                                                      packet.captured_at, packet.fps, live,
                                                      source_size=packet.source_size)
            else:
                for kind, image in (("color", packet.color), ("depth", packet.depth_rgb)):
                    result[f"{slot}_{kind}"] = StreamFrame(
                        image, False, packet.number, packet.received_at, packet.captured_at, packet.fps, live,
                        packet.depth if kind == "depth" else None, (packet.near, packet.far) if kind == "depth" else None)
        return result

    def metadata(self):
        """Device info of the running RealSense extras, keyed by slot (session.json "devices")."""
        return {slot: camera.snapshot()[1] for slot, camera in self.cameras().items()
                if slot in self.wanted and isinstance(camera, RealSenseCamera)}

    def drain_errors(self):
        """Per-camera error / recovery events since the last call: [(slot, kind, value)]."""
        events = []
        for slot, camera in self.cameras().items():
            while not camera.events.empty():
                kind, value = camera.events.get()
                if kind in ("error", "info"):
                    events.append((slot, kind, value))
        return events

    def save_snapshot(self, slots, folder=None):
        """Original-resolution frames of `slots` into `folder` (created if None): the webcam's own JPEG,
        D435 colour/depth PNG plus raw depth .npy."""
        now = time.monotonic()
        cameras = self.cameras()
        written = {}
        for slot in slots:
            camera = cameras.get(slot)
            if isinstance(camera, Webcam):
                latest = camera.jpeg()
                if latest and now - latest[2] < 2:
                    folder = folder or new_folder(CAPTURES)
                    (folder / f"{slot}_color.jpg").write_bytes(latest[0])
                    written[f"{slot}_color"] = {"frame_number": latest[1], "file": f"{slot}_color.jpg",
                                                "resolution": camera.snapshot()[1].get("resolution")}
            elif isinstance(camera, RealSenseCamera):
                try:
                    packet = camera.full_packet() if camera.running else None
                except RuntimeError:
                    logging.exception("Full-resolution frame of %s failed", slot)
                    packet = None
                if packet and now - packet.received_at < 2:
                    folder = folder or new_folder(CAPTURES)
                    Image.fromarray(packet.color).save(folder / f"{slot}_color.png")
                    Image.fromarray(packet.depth_rgb).save(folder / f"{slot}_depth.png")
                    np.save(folder / f"{slot}_depth_raw.npy", packet.depth)
                    for kind in ("color", "depth"):
                        written[f"{slot}_{kind}"] = {"frame_number": packet.number, "captured_at": packet.captured_at,
                                                     "resolution": [packet.color.shape[1], packet.color.shape[0]]}
        if not written:
            return folder
        metadata_path = folder / "metadata.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            metadata = {}
        metadata["extra_cameras"] = {slot: cameras[slot].snapshot()[1] for slot in slots if slot in cameras}
        metadata.setdefault("streams", {}).update(written)
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return folder
