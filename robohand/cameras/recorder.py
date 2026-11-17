"""Multi-track H.264 and lossless depth recording for RealSense RGB-D cameras."""
from __future__ import annotations

from datetime import datetime
import csv
import json
import logging
import math
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time

import cv2
import imageio_ffmpeg
import numpy as np

from .hub import STREAM_NAMES, StreamFrame, new_folder

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class TrackWriter:
    """Bounded queue; gaps repeat the last frame to preserve the real timeline."""
    def __init__(self, key, initial, folder, start, fps, failed):
        self.key, self.initial, self.folder = key, initial, folder
        self.start, self.fps, self.failed = start, fps, failed
        self.queue = queue.Queue(maxsize=8)
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"Encode-{key}")
        self.frames_written = self.duplicates = self.missing = self.queue_dropped = 0
        self.error = None
        self.last_id = None
        self.ready = threading.Event()
        self.stats = {}

    def start_worker(self):
        self.thread.start()

    def submit(self, index, frame):
        if self.error:
            return
        try:
            self.queue.put_nowait((index, frame))
        except queue.Full:
            self.queue_dropped += 1

    def finish(self, count):
        while self.thread.is_alive():
            try:
                self.queue.put((count, None), timeout=.2)
                return
            except queue.Full:
                continue

    def _run(self):
        process = None
        stderr = None
        raw_process = raw_stderr = None
        raw_name = None
        try:
            h,w = self.initial.image.shape[:2]
            output = self.folder / f"{self.key}.mp4"
            stderr = (self.folder / f"{self.key}_encoder.log").open("wb")
            command = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
                       "-f", "rawvideo", "-pixel_format", "bgr24", "-video_size", f"{w}x{h}",
                       "-framerate", str(self.fps), "-i", "pipe:0", "-an", "-c:v", "libx264",
                       "-preset", "veryfast", "-crf", "20", "-threads", "2", "-pix_fmt", "yuv420p",
                       "-movflags", "+faststart", str(output)]
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                       stderr=stderr, creationflags=NO_WINDOW)
            if self.key.endswith("_depth") and self.initial.raw_depth is not None:
                raw_name = f"{self.key}_raw.mkv"
                raw_stderr = (self.folder / f"{self.key}_raw_encoder.log").open("wb")
                raw_command = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
                               "-f", "rawvideo", "-pixel_format", "gray16le", "-video_size", f"{w}x{h}",
                               "-framerate", str(self.fps), "-i", "pipe:0", "-an", "-c:v", "ffv1",
                               "-level", "3", "-threads", "2", "-pix_fmt", "gray16le", str(self.folder/raw_name)]
                raw_process = subprocess.Popen(raw_command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                               stderr=raw_stderr, creationflags=NO_WINDOW)
            self.ready.set()
            with (self.folder / f"{self.key}_timestamps.csv").open("w", newline="", encoding="utf-8") as csvfile:
                rows = csv.writer(csvfile)
                rows.writerow(["output_frame", "video_time_s", "source_frame", "source_received_monotonic_s",
                               "source_wall_time", "source_age_ms", "repeated_frame", "source_unavailable",
                               "depth_display_near_m", "depth_display_far_m"])
                previous = self.initial
                cached_number, cached_bytes = None, None
                cached_raw_number, cached_raw_bytes = None, None
                raw_blank = np.zeros((h,w), dtype=np.uint16).tobytes() if raw_process else None
                blank = np.zeros((h,w,3), dtype=np.uint8)
                cv2.putText(blank, "CAMERA DISCONNECTED", (24, max(50,h//2)),
                            cv2.FONT_HERSHEY_SIMPLEX, 1 if w<1000 else 2, (80,80,235), 2)
                blank_bytes = blank.tobytes()
                def emit(frame, index):
                    nonlocal cached_number, cached_bytes
                    nonlocal cached_raw_number, cached_raw_bytes
                    unavailable = not frame.live
                    identity = (frame.number, frame.received_at)
                    repeated = identity == self.last_id
                    if unavailable:
                        data = blank_bytes
                    elif cached_number != identity:
                        image = frame.image if frame.bgr else cv2.cvtColor(frame.image, cv2.COLOR_RGB2BGR)
                        if image.shape[:2] != (h,w):
                            raise RuntimeError("녹화 중 영상 해상도가 변경됐습니다.")
                        cached_bytes, cached_number = image.tobytes(), identity
                        data = cached_bytes
                    else:
                        data = cached_bytes
                    process.stdin.write(data)
                    if raw_process:
                        if unavailable:
                            raw_data = raw_blank
                        elif frame.raw_depth is None:
                            raise RuntimeError("프레임의 원본 깊이가 없습니다. 녹화를 중지합니다.")
                        elif cached_raw_number != identity:
                            if frame.raw_depth.dtype != np.uint16 or frame.raw_depth.shape != (h,w):
                                raise RuntimeError("원본 깊이 형식이 올바르지 않습니다.")
                            cached_raw_bytes = frame.raw_depth.astype("<u2", copy=False).tobytes()
                            cached_raw_number = identity
                            raw_data = cached_raw_bytes
                        else:
                            raw_data = cached_raw_bytes
                        raw_process.stdin.write(raw_data)
                    age = max(0, (self.start+index/self.fps-frame.received_at)*1000)
                    rows.writerow([index, f"{index/self.fps:.6f}", frame.number,
                                   f"{frame.received_at:.6f}", frame.captured_at, f"{age:.2f}",
                                   int(repeated), int(unavailable), *(frame.depth_range or ("", ""))])
                    self.frames_written += 1
                    self.duplicates += int(repeated)
                    self.missing += int(unavailable)
                    self.last_id = identity
                while True:
                    index, frame = self.queue.get()
                    while self.frames_written < index:
                        emit(previous, self.frames_written)
                    if frame is None:
                        break
                    if self.frames_written == index:
                        emit(frame, index)
                    previous = frame
            process.stdin.close()
            if raw_process:
                raw_process.stdin.close()
            if process.wait(timeout=30) != 0:
                raise RuntimeError(f"{self.key} MP4 인코더 오류. 저장 폴더의 로그를 확인하세요.")
            if raw_process and raw_process.wait(timeout=30) != 0:
                raise RuntimeError("16비트 원본 깊이 인코더 오류. 저장 폴더의 로그를 확인하세요.")
            if output.stat().st_size < 1024:
                raise RuntimeError(f"{self.key} 녹화 파일을 생성하지 못했습니다.")
            self.stats = {"file": output.name, "camera_key": self.key.rsplit("_", 1)[0],
                          "resolution": [w,h], "codec": "H.264", "fps": self.fps,
                          "frames": self.frames_written, "duration_s": self.frames_written/self.fps,
                          "repeated_frames": self.duplicates, "unavailable_frames": self.missing,
                          "queue_dropped_samples": self.queue_dropped, "bytes": output.stat().st_size}
            if raw_name:
                self.stats.update(raw_depth_file=raw_name, raw_depth_codec="FFV1",
                                  raw_depth_dtype="uint16", raw_depth_alignment="color",
                                  raw_depth_bytes=(self.folder/raw_name).stat().st_size)
        except Exception as error:
            logging.exception("Recording track %s failed", self.key)
            self.error = str(error)
            self.failed(self.key, self.error)
        finally:
            self.ready.set()
            for encoder in [process,raw_process]:
                if encoder is not None and encoder.poll() is None:
                    try:
                        encoder.stdin.close()
                    except (OSError, ValueError):
                        pass
                    try:
                        encoder.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        encoder.kill()
                        encoder.wait()
            for logfile in [stderr,raw_stderr]:
                if logfile:
                    logfile.close()


class Recorder:
    def __init__(self, hub):
        self.hub = hub
        self.events = queue.SimpleQueue()
        self.thread = None
        self.stop_event = threading.Event()
        self.recording = False
        self.started_at = None
        self.folder = None
        self.tracks = {}
        self.error = None
        self.result = None
        self.requested_stop_at = None
        self.sources = {}

    @property
    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    SOURCE_NAMES = {"manus": "MANUS 글러브", "vive": "VIVE 트래커"}

    def start(self, keys, parent, fps=30, sources=None):
        """``sources``: {"manus": ManusGlove, "vive": ViveTracker, ...} recorded into the same
        session folder on the same monotonic clock (each provides status/begin/end)."""
        if self.busy:
            return
        if not keys:
            raise ValueError("녹화할 영상을 하나 이상 선택하세요.")
        sources = {name: source for name, source in (sources or {}).items() if source is not None}
        for name, source in sources.items():
            status = source.status()
            if not status["live"]:
                raise RuntimeError(f"{self.SOURCE_NAMES.get(name, name)} 실시간 데이터 없음: {status['message']}")
        self.stop_event.clear()
        self.recording, self.error, self.result = False, None, None
        self.requested_stop_at, self.started_at, self.tracks = None, None, {}
        self.sources = sources
        self.thread = threading.Thread(target=self._run, args=(list(keys), Path(parent), fps),
                                       daemon=True, name="Recording-session")
        self.thread.start()

    def stop(self):
        if not self.stop_event.is_set():
            self.requested_stop_at = time.monotonic()
        self.stop_event.set()

    def _failed(self, key, error):
        self.error = f"{STREAM_NAMES[key]}: {error}"
        self.stop()

    def _run(self, keys, parent, fps):
        manifest = {}
        count = 0
        try:
            frames = self.hub.frames()
            unavailable = [STREAM_NAMES[k] for k in keys if k not in frames or not frames[k].live]
            if unavailable:
                raise RuntimeError("실시간 영상이 없습니다: " + ", ".join(unavailable))
            for key in keys:
                if key.endswith("_depth") and frames[key].raw_depth is None:
                    raise RuntimeError(f"{STREAM_NAMES[key]} 원본 깊이값을 수신하지 못했습니다.")
            parent.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(parent).free < 512*1024*1024:
                raise RuntimeError("녹화 저장 공간이 부족합니다. 512 MB 이상 확보하세요.")
            self.folder = new_folder(parent)
            manifest = {"format_version": 3, "status": "recording", "started_at": datetime.now().astimezone().isoformat(),
                        "output_fps": fps, "audio": False, "devices": self.hub.metadata(),
                        "selected_streams": keys,
                        "timing": "Common host monotonic clock; nearest latest sample, repeat gaps; not hardware synchronized",
                        "depth_video": "Colorized MP4 plus lossless uint16 FFV1 sidecar for 3D replay",
                        "depth_display_range_m": list(frames["d405_depth"].depth_range)
                                                 if "d405_depth" in frames and frames["d405_depth"].depth_range else None,
                        "depth_display_ranges_m": {key.rsplit("_", 1)[0]: list(frames[key].depth_range)
                                                   for key in keys if key.endswith("_depth") and frames[key].depth_range},
                        "depth_units": "uint16 sample * devices[camera_key].depth_scale_m = axial depth in meters",
                        "depth_invalid_values": [0, 65535],
                        "tracks": {}}
            self._manifest(manifest)
            # Encoders initialize without camera locks; capture continues independently.
            start = time.monotonic()
            self.tracks = {k: TrackWriter(k, frames[k], self.folder, start, fps, self._failed) for k in keys}
            for track in self.tracks.values():
                track.start_worker()
            for track in self.tracks.values():
                if not track.ready.wait(10):
                    raise RuntimeError("녹화 인코더 준비 시간이 초과됐습니다.")
            if self.error:
                raise RuntimeError(self.error)
            self.started_at = time.monotonic()
            manifest["start_monotonic_s"] = self.started_at
            for track in self.tracks.values():
                track.start = self.started_at
            for source in self.sources.values():
                source.begin(self.folder, self.started_at)
            self.recording = True
            self.events.put(("started", str(self.folder)))
            last_index = -1
            while not self.stop_event.is_set():
                elapsed = time.monotonic()-self.started_at
                index = int(elapsed*fps)
                if index > last_index:
                    current = self.hub.frames()
                    for key, track in self.tracks.items():
                        frame = current.get(key)
                        if frame is None:
                            old = track.initial
                            frame = StreamFrame(old.image, old.bgr, old.number, old.received_at,
                                                old.captured_at, 0, False)
                        track.submit(index, frame)
                        if index-track.frames_written > fps*5:
                            raise RuntimeError("인코더가 녹화 속도를 따라가지 못해 녹화를 종료합니다.")
                    last_index = index
                if index % (fps*5) == 0 and shutil.disk_usage(parent).free < 100*1024*1024:
                    raise RuntimeError("저장 공간이 부족해 녹화를 종료합니다.")
                self.stop_event.wait(max(.001, (last_index+1)/fps-(time.monotonic()-self.started_at)))
            end = self.requested_stop_at or time.monotonic()
            count = max(1, last_index+1, math.ceil(max(0, end-self.started_at)*fps))
        except Exception as error:
            logging.exception("Recording session failed")
            self.error = self.error or str(error)
            if self.started_at:
                count = max(count, math.ceil((time.monotonic()-self.started_at)*fps))
        finally:
            self.recording = False
            summaries = {}
            for name, source in self.sources.items():
                try:
                    summaries[name] = source.end()
                except Exception as error:
                    logging.exception("%s recording finalize failed", name)
                    summaries[name] = {"error": str(error)}
            self.events.put(("finalizing", None))
            for track in self.tracks.values():
                track.finish(count)
            for track in self.tracks.values():
                track.thread.join()
            if manifest:
                warnings = [f"{STREAM_NAMES[k]}: {t.missing} frames unavailable"
                            for k,t in self.tracks.items() if t.missing]
                for name, summary in summaries.items():
                    if summary is None:
                        continue  # session failed before this source started
                    manifest[name] = summary
                    label = self.SOURCE_NAMES.get(name, name)
                    if summary.get("error"):
                        warnings.append(f"{label}: {summary['error']}")
                    if summary.get("dropped_records"):
                        warnings.append(f"{label}: {summary['dropped_records']} records dropped")
                    if not summary.get("records"):
                        warnings.append(f"{label}: no data recorded")
                manifest.update(status="error" if self.error else "complete", error=self.error, warnings=warnings,
                                ended_at=datetime.now().astimezone().isoformat(),
                                duration_s=count/fps, tracks={k: {**t.stats, "error": t.error}
                                                            for k,t in self.tracks.items()})
                try:
                    self._manifest(manifest)
                except Exception as error:
                    self.error = f"세션 정보 저장 실패: {error}"
                    logging.exception("Manifest write failed")
            self.result = manifest
            self.events.put(("error", self.error) if self.error else ("saved", str(self.folder)))

    def _manifest(self, data):
        temp = self.folder / "session.tmp"
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.folder / "session.json")
