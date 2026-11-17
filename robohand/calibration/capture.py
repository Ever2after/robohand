"""Collect high-resolution stationary ChArUco views from three cameras."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import time

import numpy as np
from PIL import Image

from ..cameras.d405 import Camera
from ..cameras.hub import CAMERA_KEYS
from ..settings import read_settings


def capture(folder, count=16, resolution=(1280, 720), fps=5, progress=None):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    serials = read_settings()["realsense_serials"]
    cameras = {key: Camera(serial, resolution=resolution, fps=fps)
               for key,serial in zip(CAMERA_KEYS, serials)}
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("folder",type=Path)
    parser.add_argument("--count",type=int,default=16)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=5)
    arguments = parser.parse_args()
    capture(arguments.folder, arguments.count, (arguments.width, arguments.height), arguments.fps)
