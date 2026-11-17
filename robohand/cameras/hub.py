"""Serial-number based capture for three independent RealSense RGB-D cameras."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import logging
from pathlib import Path
import queue
import threading
import time

import cv2
import numpy as np
from PIL import Image
import pyrealsense2 as rs

from ..paths import CAPTURES
from ..settings import read_settings
from .d405 import Camera as D405Camera

cv2.setNumThreads(2)
CAMERA_KEYS = ("d405", "d405_2", "d405_3")
CAMERA_NAMES = {key: f"RGB-D {index}" for index, key in enumerate(CAMERA_KEYS, 1)}
STREAM_NAMES = {
    f"{key}_{kind}": f"{CAMERA_NAMES[key]} {caption}"
    for key in CAMERA_KEYS
    for kind, caption in [("color", "컬러"), ("depth", "깊이")]
}


def new_folder(parent):
    folder = Path(parent) / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    folder.mkdir(parents=True)
    return folder


@dataclass(frozen=True)
class StreamFrame:
    image: np.ndarray
    bgr: bool
    number: int
    received_at: float
    captured_at: str
    fps: float
    live: bool
    raw_depth: np.ndarray | None = None
    depth_range: tuple | None = None


class CameraHub:
    def __init__(self):
        self.lock = threading.RLock()
        self.devices = {key: D405Camera() for key in CAMERA_KEYS}
        self.available = []
        self.events = queue.SimpleQueue()
        self.scanning = False
        self.cancel_scan = threading.Event()
        self.scan_thread = None

    @property
    def d405(self):
        return self.devices["d405"]

    def cameras(self):
        with self.lock:
            return dict(self.devices)

    def packets(self):
        return {key: camera.snapshot()[0] for key, camera in self.cameras().items()}

    def start(self, preferred_serials=None):
        with self.lock:
            if self.scanning:
                return
            self.scanning = True
        self.cancel_scan.clear()

        def discover():
            try:
                context = rs.context()
                available = []
                for device in context.query_devices():
                    name = device.get_info(rs.camera_info.name)
                    if "D405" in name:
                        available.append({
                            "name": name,
                            "serial": device.get_info(rs.camera_info.serial_number),
                            "usb": device.get_info(rs.camera_info.usb_type_descriptor),
                        })
                available.sort(key=lambda item: item["serial"])
                settings = read_settings()
                preferences = preferred_serials if preferred_serials is not None else settings.get("realsense_serials", [])
                selected = list((preferences or [])[:3])
                while len(selected) < 3:
                    remaining = next((item["serial"] for item in available if item["serial"] not in selected), None)
                    selected.append(remaining)
                actual = [serial for serial in selected if serial]
                if len(actual) != len(set(actual)):
                    raise ValueError("각 RGB-D 슬롯에는 서로 다른 카메라를 선택하세요.")
                by_serial = {item["serial"]: item for item in available}
                ranges = settings.get("depth_ranges_mm", {})
                for key, serial in zip(CAMERA_KEYS, selected):
                    if self.cancel_scan.is_set():
                        return
                    previous = self.cameras()[key]
                    if previous.running and previous.serial != serial:
                        raise RuntimeError("장치 배치 변경 전에 전체 연결을 종료하세요.")
                    if previous.serial == serial:
                        camera = previous
                    else:
                        camera = D405Camera(serial)
                    default_far = settings.get("far_mm", 500) if key == "d405" else 2000
                    near, far = ranges.get(key, [settings.get("near_mm", 70), default_far])
                    camera.set_range(float(near)/1000, float(far)/1000)
                    camera.usb2_fps = 15
                    with self.lock:
                        self.devices[key] = camera
                    if serial and not self.cancel_scan.is_set():
                        camera.start()
                        self.cancel_scan.wait(.15)
                self.available = available
                chosen = [by_serial.get(serial, {"name": "D405", "serial": serial, "usb": "미연결"})
                          if serial else None for serial in selected]
                self.events.put(("devices", chosen))
            except Exception as error:
                logging.exception("RealSense discovery failed")
                self.events.put(("error", str(error)))
            finally:
                self.scanning = False

        self.scan_thread = threading.Thread(target=discover, daemon=True, name="RealSense-discovery")
        self.scan_thread.start()

    def stop(self):
        self.cancel_scan.set()
        for camera in self.cameras().values():
            camera.stop()

    @property
    def running(self):
        return self.scanning or any(camera.running for camera in self.cameras().values())

    def join(self, timeout=10):
        deadline = time.monotonic() + timeout
        if self.scan_thread:
            self.scan_thread.join(max(0, deadline-time.monotonic()))
        for camera in self.cameras().values():
            if camera.thread:
                camera.thread.join(max(0, deadline-time.monotonic()))

    def frames(self, packets=None):
        now = time.monotonic()
        result = {}
        for key, camera in self.cameras().items():
            packet = packets.get(key) if packets is not None else camera.snapshot()[0]
            if packet is None:
                continue
            live = camera.running and not camera.stop_event.is_set() and now-packet.received_at < 2
            for kind, image in [("color", packet.color), ("depth", packet.depth_rgb)]:
                result[f"{key}_{kind}"] = StreamFrame(
                    image, False, packet.number, packet.received_at, packet.captured_at,
                    packet.fps, live, packet.depth if kind == "depth" else None,
                    (packet.near, packet.far) if kind == "depth" else None,
                )
        return result

    def metadata(self):
        return {key: camera.snapshot()[1] for key, camera in self.cameras().items()}

    def save_snapshot(self):
        packets = self.packets()
        views = {key: frame for key, frame in self.frames(packets).items() if frame.live}
        if not views:
            raise RuntimeError("저장할 실시간 영상이 없습니다.")
        folder = new_folder(CAPTURES)
        for key, frame in views.items():
            Image.fromarray(frame.image).save(folder / f"{key}.png")
        for key, packet in packets.items():
            if packet is not None and f"{key}_depth" in views:
                Image.fromarray(packet.depth).save(folder / f"{key}_depth_raw.png")
                np.save(folder / f"{key}_depth_raw.npy", packet.depth)
        metadata = {
            "devices": self.metadata(),
            "streams": {key: {"captured_at": frame.captured_at, "frame_number": frame.number,
                              "resolution": [frame.image.shape[1], frame.image.shape[0]]}
                        for key, frame in views.items()},
            "depth_scale_m": packets["d405"].scale if packets.get("d405") else None,
        }
        (folder / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return folder
