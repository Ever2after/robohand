"""One RealSense RGB-D camera (D405 by default, also D435): colour + colour-aligned depth on a thread."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
import logging
import queue
import threading
import time

import cv2
import numpy as np
import pyrealsense2 as rs


def make_lut():
    stops = np.array([[255, 225, 120], [255, 122, 82], [192, 67, 172],
                      [87, 82, 205], [56, 153, 224], [64, 218, 213]])
    return np.stack([np.interp(np.linspace(0, 5, 256), np.arange(6), stops[:, k])
                     for k in range(3)], axis=1).astype(np.uint8)


LUT = make_lut()


COLORMAP = LUT.reshape(256, 1, 3)
BACKGROUND = (9, 17, 26)


def colorize(depth, scale, near, far):
    """Depth (uint16 samples) -> RGB palette image and % of pixels inside [near, far] metres.

    Only OpenCV calls on whole images (they release the GIL), so several cameras can colorize
    1080p depth in parallel threads at full frame rate."""
    low = max(1, int(np.ceil(near / scale - 1e-6)))
    high = min(65534, int(np.floor(far / scale + 1e-6)))
    inside = cv2.inRange(depth, low, high)
    alpha = 255 / ((far - near) / scale)
    index = cv2.convertScaleAbs(depth, alpha=alpha, beta=-near / scale * alpha)
    rgb = np.empty((*depth.shape, 3), np.uint8)
    rgb[:] = BACKGROUND
    cv2.copyTo(cv2.applyColorMap(index, COLORMAP), inside, rgb)
    return rgb, cv2.countNonZero(inside) * 100 / depth.size


def distance_mm(depth, scale, x, y):
    """Median of valid samples in a 5x5 patch, in camera-axis millimeters."""
    patch = depth[max(0, y-2):y+3, max(0, x-2):x+3]
    values = patch[(patch > 0) & (patch < 65535)]
    return float(np.median(values) * scale * 1000) if values.size else None


def intrinsics_dict(profile):
    i = profile.as_video_stream_profile().get_intrinsics()
    return {"width": i.width, "height": i.height, "fx": i.fx, "fy": i.fy,
            "ppx": i.ppx, "ppy": i.ppy, "model": str(i.model), "coeffs": list(i.coeffs)}


@dataclass
class Frame:
    """One aligned colour + depth sample. The depth palette image (`depth_rgb`, `coverage`) is computed on
    first use and cached, so the capture thread only receives and aligns; only the dashboard preview
    needs it (recordings store the raw depth). Treat frames as read-only."""
    color: np.ndarray
    depth: np.ndarray
    scale: float
    fps: float
    number: int
    captured_at: str
    received_at: float
    near: float
    far: float
    palette: tuple | None = field(default=None, repr=False, compare=False)   # (depth_rgb, coverage) cache
    capture_time: float | None = None   # exposure time on the time.monotonic clock (None: use received_at)

    @property
    def depth_rgb(self):
        if self.palette is None:
            self.palette = colorize(self.depth, self.scale, self.near, self.far)
        return self.palette[0]

    @property
    def coverage(self):
        self.depth_rgb
        return self.palette[1]


class Camera:
    def __init__(self, serial=None, resolution=(640, 480), fps=None, model="D405", color_resolution=None,
                 usb2_resolution=None):
        """`resolution` is the depth stream; `color_resolution` (default: the same) is the colour stream, and
        depth is aligned to it, so every frame and the recorded tracks have the colour size. On a USB 2 link
        `usb2_resolution` (if given) replaces both, since high-resolution profiles need USB 3."""
        self.serial = serial
        self.model = model  # substring of the RealSense product name, e.g. "D405" or "D435"
        self.resolution = resolution
        self.color_resolution = color_resolution or resolution
        self.usb2_resolution = usb2_resolution
        self.requested_fps = fps
        self.lock = threading.Lock()
        self.events = queue.SimpleQueue()
        self.thread = None
        self.stop_event = threading.Event()
        self.latest = None
        self.history = deque(maxlen=10)   # newest packets (recording picks the one nearest each tick)
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
            self.history.clear()
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"{self.model}-{self.serial or 'default'}")
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def snapshot(self):
        with self.lock:
            return self.latest, dict(self.info)

    def recent(self):
        with self.lock:
            return list(self.history)

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
            depth_size, color_size = self.resolution, self.color_resolution
            if usb.startswith('2') and self.usb2_resolution:
                depth_size = color_size = self.usb2_resolution
            width, height = color_size
            config = rs.config()
            config.enable_device(device.get_info(rs.camera_info.serial_number))
            config.enable_stream(rs.stream.depth, *depth_size, rs.format.z16, fps)
            config.enable_stream(rs.stream.color, *color_size, rs.format.rgb8, fps)
            profile = pipeline.start(config)
            started = True
            scale = profile.get_device().first_depth_sensor().get_depth_scale()
            info = {"name": device.get_info(rs.camera_info.name),
                    "serial": device.get_info(rs.camera_info.serial_number),
                    "firmware": device.get_info(rs.camera_info.firmware_version),
                    "usb": device.get_info(rs.camera_info.usb_type_descriptor),
                    "depth_scale_m": scale,
                    "color_intrinsics": intrinsics_dict(profile.get_stream(rs.stream.color)),
                    "depth_alignment": "color", "resolution": [width, height],
                    "depth_sensor_resolution": list(depth_size), "target_fps": fps}
            with self.lock:
                self.info = info
            self.events.put(("info", info))
            align = rs.align(rs.stream.color)
            times = deque(maxlen=60)
            # Receiving and processing run on separate threads with a short backlog between them: the
            # SDK keeps only the newest frameset, so a processing hiccup (align is ~10 ms at 1080p and the
            # CPU is shared with encoders) would otherwise drop frames outright.
            backlog, arrived, grab_error = deque(), threading.Condition(), []

            def grab():
                failures = 0
                while not self.stop_event.is_set():
                    try:
                        frames = pipeline.wait_for_frames(1200)
                    except RuntimeError:
                        if self.stop_event.is_set():
                            break
                        failures += 1
                        if failures >= 3:
                            grab_error.append("영상 수신이 끊겼습니다. USB 연결과 다른 카메라 앱을 확인하세요.")
                            break
                        continue
                    failures = 0
                    arrival = time.monotonic()
                    # Frame time on the monotonic clock: global / system-time stamps are host-clock
                    # milliseconds (exposure, resp. arrival at the host), so their age dates the frame.
                    depth_frame = frames.get_depth_frame()
                    if depth_frame and depth_frame.get_frame_timestamp_domain() in (
                            rs.timestamp_domain.global_time, rs.timestamp_domain.system_time):
                        age = time.time() - depth_frame.get_timestamp() / 1000
                        if 0 <= age < 1:
                            arrival -= age
                    frames.keep()
                    with arrived:
                        backlog.append((frames, arrival))
                        while len(backlog) > 8:     # processing fell >0.25 s behind: drop the oldest
                            backlog.popleft()
                        arrived.notify()
                with arrived:
                    arrived.notify()

            grabber = threading.Thread(target=grab, daemon=True, name=f"{self.model}-{self.serial}-grab")
            grabber.start()
            while not self.stop_event.is_set():
                with arrived:
                    if not backlog and not grab_error and grabber.is_alive():
                        arrived.wait(1.5)
                    item = backlog.popleft() if backlog else None
                if item is None:
                    if grab_error:
                        raise RuntimeError(grab_error[0])
                    if not grabber.is_alive():
                        break
                    continue
                frames, capture_time = item
                frames = align.process(frames)
                depth_frame, color_frame = frames.get_depth_frame(), frames.get_color_frame()
                if not depth_frame or not color_frame:
                    continue
                depth = np.asanyarray(depth_frame.get_data()).copy()
                color = np.asanyarray(color_frame.get_data()).copy()
                now = time.monotonic()
                times.append(capture_time)
                fps = (len(times)-1) / (times[-1]-times[0]) if len(times) > 1 and times[-1] > times[0] else 0.0
                with self.lock:
                    near, far = self.near, self.far
                packet = Frame(color, depth, scale, fps, depth_frame.get_frame_number(),
                               datetime.now().astimezone().isoformat(), now, near, far,
                               capture_time=capture_time)
                with self.lock:
                    self.latest = packet
                    self.history.append(packet)
            grabber.join(3)
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

