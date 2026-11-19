"""Preview cameras beside the three D405s: RealSense D435 RGB-D and 4K UVC webcams.

Only the group shown in Camera Studio runs, so four 4K MJPEG decoders and the extra
RealSense streams do not compete with the D405s for USB bandwidth and CPU all the time.
These cameras are not part of the synchronized recording yet.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime
import json
import logging
import queue
import threading
import time

import cv2
import numpy as np
from PIL import Image
import pyrealsense2 as rs

from ..paths import CAPTURES
from .d405 import Camera as RealSenseCamera
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
WEBCAM_SIZE = (3840, 2160)
WEBCAM_FPS = 30


def list_webcams():
    """UVC cameras that are not RealSense nodes, in a stable (device path) order."""
    from cv2_enumerate_cameras import enumerate_cameras
    cameras = [camera for camera in enumerate_cameras(cv2.CAP_MSMF)
               if "realsense" not in camera.name.lower() and camera.vid != 0x8086]
    return sorted(cameras, key=lambda camera: camera.path.lower())


class Webcam:
    """One UVC camera via Media Foundation (DirectShow only reaches ~15 fps at 4K MJPEG)."""

    def __init__(self, index, path, name):
        # Media Foundation enumeration is unreliable from worker threads, so the index is resolved
        # during discovery (rerun on every tab switch, which also follows replugging).
        self.index, self.path, self.name = index, path, name
        self.lock = threading.Lock()
        self.events = queue.SimpleQueue()
        self.thread = None
        self.stop_event = threading.Event()
        self.latest = None
        self.info = {"name": name, "path": path}

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        if self.running:
            return
        self.stop_event.clear()
        with self.lock:
            self.latest = None
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"Webcam-{self.path[-12:]}")
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def snapshot(self):
        with self.lock:
            return self.latest, dict(self.info)

    def _run(self):
        cap = None
        try:
            cap = cv2.VideoCapture(self.index, cv2.CAP_MSMF)
            if not cap.isOpened():
                raise RuntimeError(f"{self.name}을(를) 열 수 없습니다. 다른 프로그램이 사용 중인지 확인하세요.")
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, WEBCAM_SIZE[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, WEBCAM_SIZE[1])
            cap.set(cv2.CAP_PROP_FPS, WEBCAM_FPS)
            times, number, failures = deque(maxlen=60), 0, 0
            while not self.stop_event.is_set():
                ok, image = cap.read()
                if not ok:
                    failures += 1
                    if failures >= 30:
                        raise RuntimeError(f"{self.name} 영상이 들어오지 않습니다. OBSBOT Center 등 다른 프로그램이 "
                                           "카메라를 쓰고 있으면 종료한 뒤 탭을 다시 선택하세요.")
                    time.sleep(.01)
                    continue
                failures = 0
                now = time.monotonic()
                times.append(now)
                fps = (len(times)-1) / (times[-1]-times[0]) if len(times) > 1 else 0.0
                number += 1
                if number == 1:
                    with self.lock:
                        self.info.update(resolution=[image.shape[1], image.shape[0]])
                    self.events.put(("info", dict(self.info)))
                frame = StreamFrame(image, True, number, now, datetime.now().astimezone().isoformat(), fps, True)
                with self.lock:
                    self.latest = frame
        except Exception as error:
            logging.exception("Webcam error")
            if not self.stop_event.is_set():
                self.events.put(("error", str(error)))
        finally:
            if cap is not None:
                cap.release()
            self.events.put(("stopped", None))


class ExtraCameras:
    """Discovers and runs the D435 or webcam group that is currently on screen."""

    def __init__(self):
        self.lock = threading.RLock()
        self.devices = {}          # slot -> RealSenseCamera | Webcam
        self.active = None
        self.events = queue.SimpleQueue()
        self.switching = threading.Lock()
        self.generation = 0

    def cameras(self):
        with self.lock:
            return dict(self.devices)

    @property
    def running(self):
        return any(camera.running for camera in self.cameras().values())

    def activate(self, group):
        """Show `group` ("d435", "webcam" or None): stop the other group, (re)discover and start this one."""
        with self.lock:
            self.active = group
            self.generation += 1
            generation = self.generation
        threading.Thread(target=self._switch, args=(group, generation), daemon=True, name="ExtraCameras").start()

    def stop_all(self):
        self.activate(None)

    def _switch(self, group, generation):
        with self.switching:  # one switch at a time; a newer request supersedes this one
            if generation != self.generation:
                return
            wanted = set(GROUPS[group]["slots"]) if group else set()
            for slot, camera in self.cameras().items():
                if slot not in wanted:
                    camera.stop()
            for slot, camera in self.cameras().items():
                if slot not in wanted and camera.thread:
                    camera.thread.join(5)
            if not group:
                return
            try:
                found = self._discover(group)
            except Exception as error:
                logging.exception("Extra camera discovery failed")
                self.events.put(("error", f"{GROUPS[group]['title']} 검색 실패: {error}"))
                return
            for slot in GROUPS[group]["slots"]:
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
                if camera is not None:
                    camera.start()
                    time.sleep(.15)  # stagger USB stream negotiation
            self.events.put(("devices", group, {slot: camera.snapshot()[1] | {"serial": getattr(camera, "serial", None)}
                                                for slot, camera in found.items()}))

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
                camera = RealSenseCamera(serial, model="D435")
                camera.set_range(*D435_RANGE_M)
                camera.usb2_fps = 15
                found[slot] = camera
        else:
            for slot, info in zip(WEBCAM_SLOTS, list_webcams()):
                previous = current.get(slot)
                if isinstance(previous, Webcam) and previous.path == info.path:
                    previous.index = info.index
                    found[slot] = previous
                else:
                    found[slot] = Webcam(info.index, info.path, info.name)
        return found

    def frames(self):
        """StreamFrames of the running group, keyed like the preview panels."""
        now = time.monotonic()
        result = {}
        shown = set(GROUPS[self.active]["slots"]) if self.active else set()
        for slot, camera in self.cameras().items():
            if slot not in shown:
                continue
            packet = camera.snapshot()[0]
            if packet is None:
                continue
            live = camera.running and not camera.stop_event.is_set() and now-packet.received_at < 2
            if isinstance(camera, Webcam):
                result[f"{slot}_color"] = StreamFrame(packet.image, True, packet.number, packet.received_at,
                                                      packet.captured_at, packet.fps, live)
            else:
                for kind, image in (("color", packet.color), ("depth", packet.depth_rgb)):
                    result[f"{slot}_{kind}"] = StreamFrame(
                        image, False, packet.number, packet.received_at, packet.captured_at, packet.fps, live,
                        packet.depth if kind == "depth" else None, (packet.near, packet.far) if kind == "depth" else None)
        return result

    def drain_errors(self):
        """Per-camera error / recovery events since the last call: [(slot, kind, value)]."""
        events = []
        for slot, camera in self.cameras().items():
            while not camera.events.empty():
                kind, value = camera.events.get()
                if kind in ("error", "info"):
                    events.append((slot, kind, value))
        return events

    def save_snapshot(self, folder=None):
        """Full-resolution PNG of every live stream (plus raw D435 depth) into `folder` (created if None)."""
        frames = {key: frame for key, frame in self.frames().items() if frame.live}
        if not frames:
            return folder
        folder = folder or new_folder(CAPTURES)
        for key, frame in frames.items():
            image = cv2.cvtColor(frame.image, cv2.COLOR_BGR2RGB) if frame.bgr else frame.image
            Image.fromarray(image).save(folder / f"{key}.png")
            if frame.raw_depth is not None:
                np.save(folder / f"{key}_raw.npy", frame.raw_depth)
        metadata_path = folder / "metadata.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            metadata = {}
        metadata["extra_cameras"] = {slot: camera.snapshot()[1] for slot, camera in self.cameras().items()}
        metadata.setdefault("streams", {}).update({
            key: {"captured_at": frame.captured_at, "frame_number": frame.number,
                  "resolution": [frame.image.shape[1], frame.image.shape[0]]} for key, frame in frames.items()})
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return folder
