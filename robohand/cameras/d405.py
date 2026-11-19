"""One RealSense RGB-D camera (D405 by default, also D435): colour + colour-aligned depth on a thread."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
import logging
import queue
import threading
import time

import numpy as np
import pyrealsense2 as rs


def make_lut():
    stops = np.array([[255, 225, 120], [255, 122, 82], [192, 67, 172],
                      [87, 82, 205], [56, 153, 224], [64, 218, 213]])
    return np.stack([np.interp(np.linspace(0, 5, 256), np.arange(6), stops[:, k])
                     for k in range(3)], axis=1).astype(np.uint8)


LUT = make_lut()


def colorize(depth, scale, near, far):
    meters = depth.astype(np.float32) * scale
    valid = (depth > 0) & (depth < 65535)
    inside = valid & (meters >= near) & (meters <= far)
    indices = (np.clip((meters - near) / (far - near), 0, 1) * 255).astype(np.uint8)
    rgb = LUT[indices]
    rgb[~inside] = (9, 17, 26)
    return rgb, float(np.mean(inside) * 100)


def distance_mm(depth, scale, x, y):
    """Median of valid samples in a 5x5 patch, in camera-axis millimeters."""
    patch = depth[max(0, y-2):y+3, max(0, x-2):x+3]
    values = patch[(patch > 0) & (patch < 65535)]
    return float(np.median(values) * scale * 1000) if values.size else None


def intrinsics_dict(profile):
    i = profile.as_video_stream_profile().get_intrinsics()
    return {"width": i.width, "height": i.height, "fx": i.fx, "fy": i.fy,
            "ppx": i.ppx, "ppy": i.ppy, "model": str(i.model), "coeffs": list(i.coeffs)}


@dataclass(frozen=True)
class Frame:
    color: np.ndarray
    depth: np.ndarray
    depth_rgb: np.ndarray
    scale: float
    fps: float
    number: int
    captured_at: str
    received_at: float
    coverage: float
    near: float
    far: float


class Camera:
    def __init__(self, serial=None, resolution=(640, 480), fps=None, model="D405"):
        self.serial = serial
        self.model = model  # substring of the RealSense product name, e.g. "D405" or "D435"
        self.resolution = resolution
        self.requested_fps = fps
        self.lock = threading.Lock()
        self.events = queue.SimpleQueue()
        self.thread = None
        self.stop_event = threading.Event()
        self.latest = None
        self.info = {}
        self.near, self.far = 0.07, 0.5
        self.usb2_fps = 30

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        if self.running:
            return
        self.stop_event.clear()
        with self.lock:
            self.latest = None
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"{self.model}-{self.serial or 'default'}")
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def snapshot(self):
        with self.lock:
            return self.latest, dict(self.info)

    def set_range(self, near, far):
        if not 0.01 <= near < far <= 10:
            raise ValueError("10 ≤ 가까운 거리 < 먼 거리 ≤ 10000 mm로 입력하세요.")
        with self.lock:
            self.near, self.far = near, far

    def _run(self):
        pipeline = rs.pipeline()
        started = False
        try:
            self.events.put(("state", "연결 중"))
            context = rs.context()
            devices = [d for d in context.query_devices()
                       if self.model in d.get_info(rs.camera_info.name)
                       and (self.serial is None or d.get_info(rs.camera_info.serial_number) == self.serial)]
            if not devices:
                raise RuntimeError(f"{self.model} {self.serial or ''} 연결을 확인한 뒤 다시 연결하세요.")
            device = devices[0]
            usb = device.get_info(rs.camera_info.usb_type_descriptor)
            fps = self.requested_fps or (self.usb2_fps if usb.startswith('2') else 30)
            width, height = self.resolution
            config = rs.config()
            config.enable_device(device.get_info(rs.camera_info.serial_number))
            config.enable_stream(rs.stream.depth, width, height, rs.format.z16, fps)
            config.enable_stream(rs.stream.color, width, height, rs.format.rgb8, fps)
            profile = pipeline.start(config)
            started = True
            scale = profile.get_device().first_depth_sensor().get_depth_scale()
            info = {"name": device.get_info(rs.camera_info.name),
                    "serial": device.get_info(rs.camera_info.serial_number),
                    "firmware": device.get_info(rs.camera_info.firmware_version),
                    "usb": device.get_info(rs.camera_info.usb_type_descriptor),
                    "depth_scale_m": scale,
                    "color_intrinsics": intrinsics_dict(profile.get_stream(rs.stream.color)),
                    "depth_alignment": "color", "resolution": [width, height], "target_fps": fps}
            with self.lock:
                self.info = info
            self.events.put(("info", info))
            align = rs.align(rs.stream.color)
            times = deque(maxlen=60)
            failures = 0
            while not self.stop_event.is_set():
                try:
                    frames = pipeline.wait_for_frames(1200)
                except RuntimeError:
                    if self.stop_event.is_set():
                        break
                    failures += 1
                    if failures >= 3:
                        raise RuntimeError("영상 수신이 끊겼습니다. USB 연결과 다른 카메라 앱을 확인하세요.")
                    continue
                failures = 0
                frames = align.process(frames)
                depth_frame, color_frame = frames.get_depth_frame(), frames.get_color_frame()
                if not depth_frame or not color_frame:
                    continue
                depth = np.asanyarray(depth_frame.get_data()).copy()
                color = np.asanyarray(color_frame.get_data()).copy()
                now = time.monotonic()
                times.append(now)
                fps = (len(times)-1) / (times[-1]-times[0]) if len(times) > 1 else 0.0
                with self.lock:
                    near, far = self.near, self.far
                rgb, coverage = colorize(depth, scale, near, far)
                packet = Frame(color, depth, rgb, scale, fps, depth_frame.get_frame_number(),
                               datetime.now().astimezone().isoformat(), now, coverage, near, far)
                with self.lock:
                    self.latest = packet
        except Exception as error:
            logging.exception("Camera error")
            if not self.stop_event.is_set():
                self.events.put(("error", str(error)))
        finally:
            if started:
                try:
                    pipeline.stop()
                except Exception:
                    logging.exception("Pipeline stop failed")
            self.events.put(("stopped", None))

