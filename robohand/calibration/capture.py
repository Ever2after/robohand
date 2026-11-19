"""Collect high-resolution stationary ChArUco views: the three D405s, then optionally the D435s and
webcams, one camera at a time (the board and cameras must stay still; USB bandwidth is never shared)."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import time

import numpy as np
from PIL import Image
import pyrealsense2 as rs

from ..cameras.d405 import Camera
from ..cameras.hub import CAMERA_KEYS
from ..settings import read_settings
from .intrinsics import webcam_intrinsics


def supported_fps(serial, resolution, minimum=5):
    """Lowest frame rate (>= minimum) offered for both z16 depth and rgb8 colour at `resolution`."""
    common = None
    for device in rs.context().query_devices():
        if device.get_info(rs.camera_info.serial_number) != serial:
            continue
        for stream, fmt in ((rs.stream.depth, rs.format.z16), (rs.stream.color, rs.format.rgb8)):
            rates = {profile.fps() for sensor in device.query_sensors() for profile in sensor.get_stream_profiles()
                     if profile.stream_type() == stream and profile.format() == fmt and profile.is_video_stream_profile()
                     and (profile.as_video_stream_profile().width(), profile.as_video_stream_profile().height()) == tuple(resolution)}
            common = rates if common is None else common & rates
    if not common:
        return None
    return min((rate for rate in common if rate >= minimum), default=max(common))


def capture(folder, count=16, resolution=(1280, 720), fps=5, progress=None):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    serials = read_settings()["realsense_serials"]
    cameras = {key: Camera(serial, resolution=resolution, fps=fps)
               for key,serial in zip(CAMERA_KEYS, serials) if serial}   # unassigned slot: skip, never "any D405"
    metadata = {"captured_at": datetime.now().astimezone().isoformat(),
                "profile": [*resolution, fps], "devices": {}, "samples": []}
    metadata["capture_mode"] = "sequential cameras; stationary board and cameras"
    metadata["samples"] = [{"index": index, "frames": {}} for index in range(count)]
    try:
        for key, camera in cameras.items():
            camera.start()
            deadline = time.monotonic() + 30
            previous = None
            index = 0
            while index < count and time.monotonic() < deadline:
                while not camera.events.empty():
                    kind, value = camera.events.get()
                    if kind == "error":
                        raise RuntimeError(f"{camera.serial}: {value}")
                frame, info = camera.snapshot()
                if frame is None or frame.number < 8 or frame.number == previous:
                    time.sleep(.03)
                    continue
                destination = folder / f"frame_{index:03d}"
                destination.mkdir(exist_ok=True)
                Image.fromarray(frame.color).save(destination/f"{key}_color.png")
                np.save(destination/f"{key}_depth.npy",frame.depth)
                metadata["samples"][index]["frames"][key] = {
                    "number": frame.number, "received_at": frame.received_at,
                    "captured_at": frame.captured_at}
                previous = frame.number
                index += 1
                print(f"{key}: captured {index}/{count}", flush=True)
                if progress:
                    progress(key, index, count)
            if index != count:
                raise RuntimeError(f"Not all views arrived from {camera.serial}")
            metadata["devices"][key] = info
            camera.stop()
            camera.thread.join(8)
            if camera.running:
                raise RuntimeError(f"Camera {camera.serial} did not stop")
        (folder/"capture.json").write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding="utf-8")
        return metadata
    finally:
        for camera in cameras.values():
            camera.stop()
        for camera in cameras.values():
            if camera.thread:
                camera.thread.join(8)


def _wait_new(snapshot, previous_number, deadline, number=lambda value: value.number):
    """Next value from `snapshot()` whose frame number differs from `previous_number`."""
    while time.monotonic() < deadline:
        value = snapshot()
        if value is not None and number(value) != previous_number:
            return value
        time.sleep(.03)
    return None


def capture_extras(folder, d435=None, webcams=None, count=16, resolution=(1280, 720), webcam_size=(3840, 2160),
                   progress=None):
    """Append D435 ({slot: serial}) and webcam ({slot: (path, name)}) views to an existing capture folder.
    Returns {slot: error} for cameras that could not be captured (the rest are written)."""
    from ..cameras.extra import Webcam
    folder = Path(folder)
    metadata = json.loads((folder/"capture.json").read_text(encoding="utf-8"))
    count = len(metadata["samples"])
    failed = {}
    for slot, serial in (d435 or {}).items():
        size = tuple(resolution)
        fps = supported_fps(serial, size)
        if fps is None:
            size, fps = (640, 480), supported_fps(serial, (640, 480))
        camera = Camera(serial, resolution=size, fps=fps, model="D435")
        try:
            camera.start()
            deadline, previous, index = time.monotonic() + 40, None, 0
            while index < count:
                while not camera.events.empty():
                    kind, value = camera.events.get()
                    if kind == "error":
                        raise RuntimeError(value)
                frame = _wait_new(lambda: camera.snapshot()[0], previous, deadline)
                if frame is None:
                    raise RuntimeError("영상이 들어오지 않습니다")
                previous = frame.number
                if frame.number < 8:
                    continue
                destination = folder / f"frame_{index:03d}"
                destination.mkdir(exist_ok=True)
                Image.fromarray(frame.color).save(destination/f"{slot}_color.png")
                np.save(destination/f"{slot}_depth.npy", frame.depth)
                metadata["samples"][index]["frames"][slot] = {"number": frame.number, "received_at": frame.received_at,
                                                              "captured_at": frame.captured_at}
                index += 1
                if progress:
                    progress(slot, index, count)
            metadata["devices"][slot] = camera.snapshot()[1] | {"kind": "realsense"}
        except Exception as error:
            failed[slot] = str(error)
        finally:
            camera.stop()
            if camera.thread:
                camera.thread.join(8)
    for slot, (path, name) in (webcams or {}).items():
        camera = Webcam(path, name, webcam_size)
        try:
            camera.start()
            deadline, previous, index, skipped = time.monotonic() + 40, None, 0, 0
            while index < count:
                while not camera.events.empty():
                    kind, value = camera.events.get()
                    if kind == "error":
                        raise RuntimeError(value)
                latest = _wait_new(camera.jpeg, previous, deadline, number=lambda value: value[1])
                if latest is None:
                    raise RuntimeError("영상이 들어오지 않습니다")
                previous = latest[1]
                skipped += 1
                if skipped < 15 or skipped % 3:   # let exposure settle; spread the views over time
                    continue
                destination = folder / f"frame_{index:03d}"
                destination.mkdir(exist_ok=True)
                (destination/f"{slot}_color.jpg").write_bytes(latest[0])
                metadata["samples"][index]["frames"][slot] = {"number": latest[1], "received_at": latest[2]}
                index += 1
                if progress:
                    progress(slot, index, count)
            info = camera.snapshot()[1]
            intrinsics, note = webcam_intrinsics(slot, info["resolution"], path)
            metadata["devices"][slot] = info | {"kind": "webcam", "color_intrinsics": intrinsics,
                                                "intrinsics_note": note}
        except Exception as error:
            failed[slot] = str(error)
        finally:
            camera.stop()
            if camera.thread:
                camera.thread.join(8)
    metadata["capture_mode"] = "sequential cameras (D405, D435, webcams); stationary board and cameras"
    metadata["extra_failures"] = failed
    (folder/"capture.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return failed


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("folder",type=Path)
    parser.add_argument("--count",type=int,default=16)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=5)
    arguments = parser.parse_args()
    capture(arguments.folder, arguments.count, (arguments.width, arguments.height), arguments.fps)
