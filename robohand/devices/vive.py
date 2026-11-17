"""VIVE Tracker poses from the Tracker Studio libsurvive worker, recorded on the Camera Studio clock.

Starts ``tracker_studio/survive_worker.py`` (libsurvive, no SteamVR) as a child process
with the current Python environment, parses its JSON-lines pose events and, while a
Camera Studio recording runs, writes every pose to ``vive.jsonl`` with
``t_host_s`` = time.monotonic() - session start_monotonic_s (same clock as RGB-D and MANUS).

Pairing and base-station calibration stay in Tracker Studio; disconnect here first,
because only one process can open the tracker / dongle at a time.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime
import json
import logging
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.request import urlopen

from ..paths import TRACKER_STUDIO
from .jsonl import JsonlWriter

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def tracking_state(device, now):
    """Same rule as Tracker Studio: optical < 0.25 s, otherwise inertial; stale after 0.5 s."""
    if "received" not in device:
        return "waiting"
    if device.get("kind") == "base":
        return "tracking"
    if now - device["received"] > .5:
        return "stale"
    light_age = device.get("light_age_s", -1)
    if light_age is None or light_age < 0:
        return "calibrating"
    return "tracking" if light_age < .25 else "inertial"


class ViveTracker:
    def __init__(self, studio_dir=None):
        self.studio_dir = Path(studio_dir or TRACKER_STUDIO)
        # Console python (not pythonw) so the worker's stdout pipe works from GUI parents too.
        self.python = Path(sys.executable).with_name("python.exe")
        self.worker = self.studio_dir / "survive_worker.py"
        self.lock = threading.Lock()
        self.process = None
        self.reader = None
        self.state, self.error = "꺼짐", None
        self.ready = False
        self.devices = {}             # key -> merged device info + latest pose
        self.arrivals = {}            # key -> deque of monotonic arrivals
        self.writer = None
        self.device_counts = {}
        self.logs = deque(maxlen=40)

    @property
    def available(self):
        return self.python.is_file() and self.worker.is_file()

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None

    # ------------------------------------------------------------------ lifecycle
    @staticmethod
    def studio_server_running():
        try:
            with urlopen("http://127.0.0.1:8765/api/health", timeout=.4) as response:
                return json.load(response).get("application") == "vive-tracker-studio"
        except (OSError, ValueError):
            return False

    def start(self):
        if self.running:
            return
        if not self.available:
            self.state, self.error = "없음", f"Tracker Studio를 찾을 수 없습니다: {self.studio_dir}"
            return
        self.state, self.error, self.ready = "연결 중", None, False
        threading.Thread(target=self._launch, daemon=True, name="VIVE-launch").start()

    def _launch(self):
        try:
            if self.studio_server_running():
                raise RuntimeError("Tracker Studio 서버가 트래커를 사용 중입니다 · 종료 후 다시 연결하세요")
            process = subprocess.Popen([str(self.python), "-u", str(self.worker)], cwd=self.studio_dir,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
            with self.lock:
                self.devices, self.arrivals = {}, {}
                self.process = process
            self.reader = threading.Thread(target=self._read, args=(process,), daemon=True, name="VIVE-reader")
            self.reader.start()
        except Exception as error:
            logging.exception("VIVE worker launch failed")
            self.state, self.error = "연결 실패", str(error)

    def stop(self, wait=True):
        process, self.process = self.process, None
        if process is None or process.poll() is not None:
            return
        try:
            process.stdin.write("stop\n")
            process.stdin.flush()
            if wait:
                process.wait(6)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            process.kill()
        self.state = "꺼짐"

    def _read(self, process):
        for line in process.stdout:
            now = time.monotonic()
            try:
                message = json.loads(line)
            except ValueError:
                self.logs.append(line.strip())
                continue
            event = message.get("event")
            if event == "pose":
                self._pose(message, now)
            elif event == "devices":
                with self.lock:
                    for device in message["devices"]:
                        self.devices[device["key"]] = {**self.devices.get(device["key"], {}), **device}
            elif event == "ready":
                self.ready, self.state = True, "연결됨"
            elif event == "error":
                self.error, self.state = message.get("message"), "오류"
            elif event == "log":
                self.logs.append(message.get("message", ""))
        code = process.wait()
        if self.writer:
            self.end()
        self.ready = False
        if self.process is process:  # exited on its own, not through stop()
            self.process = None
            self.state = "연결 끊김"
            self.error = self.error or f"libsurvive 프로세스가 종료됐습니다 (코드 {code}) · 클릭해 다시 연결"

    def _pose(self, message, now):
        key = message["key"]
        # Pose events repeat device metadata cached at first sight; keep the fresher heartbeat values.
        for stale in ("battery", "charging", "sensor_count", "imu_received"):
            message.pop(stale, None)
        with self.lock:
            device = {**self.devices.get(key, {}), **message, "received": now}
            device.pop("event", None)
            self.devices[key] = device
            arrivals = self.arrivals.setdefault(key, deque(maxlen=1000))
            arrivals.append(now)
            state = tracking_state(device, now)
        writer = self.writer
        if writer is not None and device.get("kind") == "tracker":
            self.device_counts[key] = self.device_counts.get(key, 0) + 1
            writer.put({"stream": "pose", "t_host_s": round(now - writer.start, 6), "key": key,
                        "serial": device.get("serial"), "pose_time_s": message.get("pose_time_s"),
                        "position": [round(v, 6) for v in message["position"]],
                        "quaternion": [round(v, 7) for v in message["quaternion"]],
                        "light_age_s": round(device.get("light_age_s", -1), 5), "tracking": state})

    # ------------------------------------------------------------------ status and recording
    def status(self):
        now = time.monotonic()
        with self.lock:
            devices = []
            for key, device in sorted(self.devices.items()):
                recent = [t for t in self.arrivals.get(key, ()) if now - t < 2]
                devices.append({"key": key, "serial": device.get("serial"), "kind": device.get("kind"),
                                "tracking": tracking_state(device, now), "hz": len(recent)/2,
                                "battery": device.get("battery"), "charging": device.get("charging"),
                                "position": device.get("position"), "quaternion": device.get("quaternion")})
        trackers = [d for d in devices if d["kind"] == "tracker"]
        bases = [d for d in devices if d["kind"] == "base"]
        live = self.running and any(d["tracking"] in ("tracking", "inertial") for d in trackers)
        if not self.running:
            message = self.error or self.state
        elif not self.ready:
            message = "libsurvive 시작 중"
        elif not trackers:
            message = "트래커를 찾는 중 · 전원/USB 확인"
        elif not live:
            message = "추적 대기 · 베이스스테이션이 보이게 두세요"
        else:
            message = f"베이스스테이션 {len(bases)}개"
        return {"running": self.running, "ready": self.ready, "live": live, "trackers": trackers,
                "bases": bases, "message": message, "state": self.state}

    def begin(self, folder, start_monotonic):
        status = self.status()
        if not status["live"]:
            raise RuntimeError(f"VIVE 트래커 추적 데이터 없음: {status['message']}")
        calibration = self.studio_dir / "runtime" / "calibration.json"
        header = {"format_version": 1, "start_monotonic_s": start_monotonic,
                  "started_at": datetime.now().astimezone().isoformat(), "source": "libsurvive (Tracker Studio worker)",
                  "time_fields": {"t_host_s": "time.monotonic() - start_monotonic_s at receipt; same clock as RGB-D",
                                  "pose_time_s": "libsurvive pose clock (s), for precise sample spacing"},
                  "coordinate_system": "libsurvive calibrated lighthouse frame; metres; Z up",
                  "quaternion_order": "w, x, y, z",
                  "tracking_states": "tracking = optical < 0.25 s old; inertial = IMU prediction",
                  "devices": [{k: d[k] for k in ("key", "serial", "kind", "position", "quaternion") if k in d}
                              for d in status["trackers"] + status["bases"]],
                  # libsurvive's config file is JSON-like but not strict JSON; keep it verbatim.
                  "lighthouse_calibration_text": calibration.read_text(encoding="utf-8", errors="replace")
                  if calibration.is_file() else None}
        self.device_counts = {}
        self.writer = JsonlWriter(Path(folder) / "vive.jsonl", start_monotonic, header, name="VIVE")

    def end(self):
        writer, self.writer = self.writer, None
        if writer is None:
            return None
        writer.close()
        return {"file": writer.path.name, "records": dict(self.device_counts), "dropped_records": writer.dropped,
                "first_t_host_s": writer.first, "last_t_host_s": writer.last, "error": writer.error}


def load_vive(folder):
    """Read ``vive.jsonl`` into per-tracker NumPy arrays."""
    import numpy as np
    header, rows = None, {}
    with (Path(folder) / "vive.jsonl").open(encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            if record.get("type") == "header":
                header = record
            elif record.get("stream") == "pose":
                rows.setdefault(record["key"], []).append(record)
    trackers = {}
    for key, samples in rows.items():
        trackers[key] = {"serial": samples[0].get("serial"),
                         "t_host_s": np.array([s["t_host_s"] for s in samples]),
                         "pose_time_s": np.array([s["pose_time_s"] for s in samples]),
                         "position": np.array([s["position"] for s in samples], dtype=np.float64),
                         "quaternion": np.array([s["quaternion"] for s in samples], dtype=np.float64),
                         "optical": np.array([s["tracking"] == "tracking" for s in samples])}
    return {"header": header, "trackers": trackers}
