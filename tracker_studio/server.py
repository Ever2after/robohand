"""Loopback-only VIVE Tracker dashboard and full-rate CSV recorder."""

import argparse
from collections import deque
import csv
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import quote, unquote, urlsplit
from urllib.request import urlopen
import uuid
import webbrowser

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
RECORDINGS = ROOT / "recordings"
CSV_COLUMNS = ["sample", "utc_time", "unix_time_s", "recording_elapsed_s", "device_id", "device_name",
               "pose_time_s", "x_m", "y_m", "z_m", "roll_x_deg", "pitch_y_deg", "yaw_z_deg",
               "qw", "qx", "qy", "qz", "tracking", "optical_age_s", "capture_id"]


def euler_degrees(quaternion):
    length = math.sqrt(sum(value * value for value in quaternion))
    w, x, y, z = (value / length for value in quaternion)
    roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    pitch = math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x))))
    yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    return [math.degrees(angle) for angle in (roll, pitch, yaw)]


class TrackerStudio:
    def __init__(self):
        RUNTIME.mkdir(exist_ok=True)
        RECORDINGS.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.control_lock = threading.Lock()
        self.process = None
        self.status = "starting"
        self.error = None
        self.receivers = []
        self.devices = {}
        self.logs = deque(maxlen=60)
        self.capture_id = ""
        self.pairing = False
        self.recording = None
        self.last_recording = None
        self.native_version = json.loads((ROOT / "build-info.json").read_text(encoding="utf-8"))["libsurvive_commit"]

    def log(self, message, level="info"):
        clean = re.sub(r"\x1b\[[0-9;]*m", "", str(message)).strip()
        if clean:
            with self.lock:
                self.logs.append({"time": datetime.now().astimezone().strftime("%H:%M:%S"),
                                  "level": level, "message": clean[:1800]})

    def connect(self, pair=False, calibrate=False):
        with self.control_lock:
            with self.lock:
                if self.recording:
                    raise ValueError("기록을 중지한 뒤 연결 또는 보정을 다시 시작하세요.")
            self.disconnect()
            with self.lock:
                self.capture_id = uuid.uuid4().hex
                self.devices = {}
                self.receivers = []
                self.status = "starting"
                self.error = None
                self.pairing = pair
            python = Path(sys.executable).with_name("python.exe")
            arguments = [str(python), "-u", str(ROOT / "survive_worker.py")]
            if pair:
                arguments.append("--pair")
            if calibrate:
                arguments.append("--calibrate")
            process = subprocess.Popen(arguments, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            with self.lock:
                self.process = process
            threading.Thread(target=self.consume, args=(process,), daemon=True).start()
            self.log("트래커 연결을 시작했습니다." + (" 페어링 대기 중입니다." if pair else ""))

    def disconnect(self):
        with self.lock:
            self.stop_recording()
            process = self.process
            self.process = None
            self.status = "disconnected"
        if process and process.poll() is None:
            try:
                process.stdin.write("stop\n")
                process.stdin.flush()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait(timeout=3)
            finally:
                process.stdin.close()

    def consume(self, process):
        for line in process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self.log(line)
                continue
            with self.lock:
                if self.process is not process:
                    continue
                event = message.get("event")
                if event == "log":
                    level = {0: "error", 1: "warning", 2: "info"}.get(message.get("level"), "info")
                    self.log(message["message"], level)
                elif event == "receivers":
                    self.receivers = message["receivers"]
                elif event == "ready":
                    self.status = "connected"
                elif event == "devices":
                    for device in message["devices"]:
                        previous = self.devices.get(device["key"], {})
                        self.devices[device["key"]] = {**previous, **device}
                elif event == "pose":
                    self.accept_pose(message)
                elif event == "error":
                    self.error = message["message"]
                    self.status = "error"
                    self.log(self.error, "error")
        code = process.wait()
        process.stdout.close()
        with self.lock:
            if self.process is process:
                self.process = None
                self.status = "error"
                self.error = self.error or f"추적 프로세스가 종료되었습니다 (코드 {code}). 연결을 다시 시도하세요."
                self.stop_recording()
                self.log(self.error, "error")

    def accept_pose(self, message):
        now = time.monotonic()
        previous = self.devices.get(message["key"], {})
        recent = previous.get("recent", deque())
        recent.append(now)
        while recent and recent[0] < now - 2:
            recent.popleft()
        device = {**previous, **message, "received": now, "recent": recent,
                  "euler": euler_degrees(message["quaternion"]), "samples": previous.get("samples", 0) + 1}
        self.devices[message["key"]] = device
        if self.recording and device["kind"] == "tracker":
            try:
                self.write_sample(device, now)
            except OSError as error:
                self.log(f"기록 파일 저장 오류 — {error}", "error")
                self.stop_recording(error=str(error))

    @staticmethod
    def tracking_state(device, now):
        if "received" not in device:
            return "waiting"
        if device["kind"] == "base":
            return "tracking"
        if now - device["received"] > 0.5:
            return "stale"
        light_age = device.get("light_age_s", -1)
        if light_age < 0:
            return "calibrating"
        return "tracking" if light_age < 0.25 else "inertial"

    def write_sample(self, device, monotonic_time):
        recording = self.recording
        recording["samples"] += 1
        received_at = time.time()
        row = [recording["samples"], datetime.fromtimestamp(received_at, timezone.utc).isoformat(timespec="milliseconds"),
               f"{received_at:.6f}", f"{monotonic_time - recording['started']:.6f}", device["serial"], device["key"],
               f"{device['pose_time_s']:.6f}", *device["position"], *device["euler"], *device["quaternion"],
               self.tracking_state(device, monotonic_time), device.get("light_age_s", -1), self.capture_id]
        recording["writer"].writerow(row)
        if monotonic_time - recording["flushed"] > 1:
            recording["file"].flush()
            recording["flushed"] = monotonic_time

    def start_recording(self, label):
        with self.lock:
            if self.recording:
                raise ValueError("이미 기록 중입니다.")
            now = time.monotonic()
            if not any(device.get("kind") == "tracker" and self.tracking_state(device, now) == "tracking"
                       for device in self.devices.values()):
                raise ValueError("광학 추적이 활성화된 트래커를 확인한 뒤 기록을 시작하세요.")
            label = re.sub(r"[^\w-]+", "_", label.strip(), flags=re.UNICODE)[:48] or "capture"
            filename = datetime.now().strftime("%Y%m%d_%H%M%S_") + label + "_" + uuid.uuid4().hex[:6] + ".csv"
            path = RECORDINGS / filename
            output = path.open("x", encoding="utf-8-sig", newline="")
            writer = csv.writer(output)
            writer.writerow(CSV_COLUMNS)
            output.flush()
            metadata = {"capture_id": self.capture_id, "created_utc": datetime.now(timezone.utc).isoformat(),
                        "libsurvive_commit": self.native_version, "position_unit": "metre", "up_axis": "Z",
                        "rotation": "right-handed Rz(yaw) Ry(pitch) Rx(roll), degrees",
                        "quaternion_order": "w,x,y,z", "columns": CSV_COLUMNS,
                        "devices": [{key: value for key, value in device.items()
                                     if key in ("key", "serial", "kind", "position", "quaternion")}
                                    for device in self.devices.values()],
                        "calibration": self.calibration_text()}
            try:
                path.with_suffix(".json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                output.close()
                raise
            self.recording = {"filename": filename, "file": output, "writer": writer,
                              "started": now, "flushed": now, "samples": 0}
            self.log(f"기록 시작 — {filename}")

    @staticmethod
    def calibration_text():
        path = RUNTIME / "calibration.json"
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else None

    def stop_recording(self, error=None):
        with self.lock:
            if not self.recording:
                return
            recording = self.recording
            self.recording = None
            try:
                recording["file"].flush()
                os.fsync(recording["file"].fileno())
            except OSError as failure:
                error = error or str(failure)
            finally:
                recording["file"].close()
            self.last_recording = {"filename": recording["filename"], "samples": recording["samples"],
                                   "duration_s": time.monotonic() - recording["started"], "error": error}
            self.log(f"기록 종료 — {recording['samples']:,}개 샘플 저장")

    def state(self):
        with self.lock:
            now = time.monotonic()
            devices = []
            for source in self.devices.values():
                device = {key: value for key, value in source.items() if key not in ("recent", "event", "received")}
                recent = source.get("recent", [])
                device["rate_hz"] = (len(recent) - 1) / (recent[-1] - recent[0]) if len(recent) > 1 and now - recent[-1] < 1 else 0
                device["age_s"] = now - source["received"] if "received" in source else None
                device["tracking"] = self.tracking_state(source, now)
                devices.append(device)
            recording = None
            if self.recording:
                recording = {key: self.recording[key] for key in ("filename", "samples")}
                recording["duration_s"] = now - self.recording["started"]
            files = sorted(RECORDINGS.glob("*.csv"), key=lambda path: path.stat().st_mtime, reverse=True)
            return {"application": "vive-tracker-studio", "status": self.status, "error": self.error,
                    "capture_id": self.capture_id, "pairing": self.pairing, "receivers": self.receivers,
                    "devices": devices, "recording": recording, "last_recording": self.last_recording,
                    "files": [{"name": path.name, "bytes": path.stat().st_size} for path in files[:30]],
                    "recordings_path": str(RECORDINGS), "logs": list(self.logs)[-20:],
                    "coordinate_system": {"up": "Z", "position": "m", "rotation": "ZYX Euler, degrees"}}


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *arguments, studio, **keywords):
        self.studio = studio
        super().__init__(*arguments, directory=str(ROOT / "web"), **keywords)

    def log_message(self, *arguments):
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            # Browsers may cancel an in-flight poll or asset download.
            pass

    def send_json(self, value, status=200):
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def trusted_host(self):
        port = self.server.server_port
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def do_GET(self):
        if not self.trusted_host():
            self.send_json({"error": "허용되지 않은 호스트입니다."}, 403)
            return
        path = urlsplit(self.path).path
        if path in ("/api/state", "/api/health"):
            self.send_json(self.studio.state())
        elif path.startswith("/api/recordings/"):
            filename = unquote(path.removeprefix("/api/recordings/"))
            if Path(filename).name != filename or not filename.endswith((".csv", ".json")):
                self.send_error(404)
                return
            target = RECORDINGS / filename
            if not target.is_file():
                self.send_error(404)
                return
            with self.studio.lock:
                if self.studio.recording and filename == self.studio.recording["filename"]:
                    self.studio.recording["file"].flush()
                content_length = target.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8" if filename.endswith(".csv") else "application/json")
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(filename))
            self.send_header("Content-Length", str(content_length))
            self.end_headers()
            with target.open("rb") as source:
                remaining = content_length
                while remaining:
                    chunk = source.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        elif path.startswith("/api/"):
            self.send_error(404)
        else:
            super().do_GET()

    def do_POST(self):
        if not self.trusted_host():
            self.send_json({"error": "허용되지 않은 호스트입니다."}, 403)
            return
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + self.headers.get("Host"):
            self.send_json({"error": "로컬 인터페이스에서 실행하세요."}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 4096:
                raise ValueError("요청 크기를 확인하세요.")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("JSON 객체를 보내세요.")
            if self.path == "/api/connect":
                self.studio.connect(pair=bool(body.get("pair")), calibrate=bool(body.get("calibrate")))
            elif self.path == "/api/disconnect":
                with self.studio.control_lock:
                    self.studio.disconnect()
            elif self.path == "/api/record/start":
                self.studio.start_recording(str(body.get("label", "capture")))
            elif self.path == "/api/record/stop":
                self.studio.stop_recording()
            else:
                self.send_error(404)
                return
            self.send_json({"ok": True})
        except (ValueError, OSError) as error:
            self.send_json({"error": str(error)}, 400)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open-browser", action="store_true")
    arguments = parser.parse_args()
    RUNTIME.mkdir(exist_ok=True)
    if sys.stdout is None:
        sys.stdout = (RUNTIME / "server.log").open("a", encoding="utf-8", buffering=1)
    if sys.stderr is None:
        sys.stderr = (RUNTIME / "server-error.log").open("a", encoding="utf-8", buffering=1)
    url = f"http://127.0.0.1:{arguments.port}"
    try:
        with urlopen(url + "/api/health", timeout=1) as response:
            if json.load(response).get("application") == "vive-tracker-studio":
                if arguments.open_browser:
                    webbrowser.open(url)
                return
    except OSError:
        pass
    studio = TrackerStudio()
    mimetypes.add_type("text/javascript", ".js")
    server = ThreadingHTTPServer(("127.0.0.1", arguments.port), partial(Handler, studio=studio))
    server.daemon_threads = True

    def stop(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    studio.connect(pair=True)
    print(f"Tracker Studio running at {url}", flush=True)
    if arguments.open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        studio.disconnect()
        server.server_close()


if __name__ == "__main__":
    main()
