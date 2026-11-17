"""MANUS Core SDK client that records glove streams on the Camera Studio clock.

Connects to the locally running MANUS Core through the ManusSDK.dll shipped with it
(remote SDK mode, read-only) and receives three streams:

* raw device data  - glove IMU quaternion and 5 raw sensor transforms (as in .mrec files)
* ergonomics       - 20 joint angles per hand in degrees
* raw skeleton     - 25 hand-skeleton node transforms per glove (meters, world coordinates)

While a Camera Studio recording runs, every callback is written to ``manus.jsonl`` with
``t_host_s`` = time.monotonic() - session start_monotonic_s, the same clock the RGB-D
tracks use, plus the MANUS publish time (UTC, 1 ms resolution) decoded by the SDK.
"""
from __future__ import annotations

from collections import deque
import ctypes
from ctypes import POINTER, byref, c_bool, c_char, c_float, c_int, c_uint8, c_uint16, c_uint32, c_uint64
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import threading
import time

from ..paths import MANUS_SDK_DIR as SDK_DIR
from .jsonl import JsonlWriter

SDK_SUCCESS = 0
SESSION_TYPE_CORE_SDK = 3
HAND_MOTION_AUTO = 4
SIDE_NAMES = {1: "left", 2: "right"}
MAX_SKELETON_NODES = 64

FINGERS = ("Thumb", "Index", "Middle", "Ring", "Pinky")
# MANUS Core 3.2 thumb naming (CMC/MCP/IP); other fingers MCP/PIP/DIP.
_JOINTS = {"Thumb": ("CMCSpread", "CMCStretch", "MCPStretch", "IPStretch")}
ERGONOMICS_NAMES = [f"{side}{finger}{joint}" for side in ("Left", "Right") for finger in FINGERS
                    for joint in _JOINTS.get(finger, ("MCPSpread", "MCPStretch", "PIPStretch", "DIPStretch"))]
CHAIN_TYPES = {5: "thumb", 6: "index", 7: "middle", 8: "ring", 9: "pinky", 13: "hand"}
JOINT_TYPES = {0: "invalid", 1: "metacarpal", 2: "proximal", 3: "intermediate", 4: "distal", 5: "tip"}
# MANUS raw skeleton layout (25 nodes) as reported by Core 3.2.1 node info; used until node info arrives.
DEFAULT_PARENTS = [0, 0, 1, 2, 3, 0, 5, 6, 7, 8, 0, 10, 11, 12, 13, 0, 15, 16, 17, 18, 0, 20, 21, 22, 23]
DEFAULT_CHAINS = ["hand"] + ["thumb"]*4 + ["index"]*5 + ["middle"]*5 + ["ring"]*5 + ["pinky"]*5
FINGER_COLORS = {"thumb": "#ff8a5c", "index": "#ffd166", "middle": "#6ee7c5", "ring": "#5cc8ff",
                 "pinky": "#c792ea", "hand": "#cfd8e3"}


def skeleton_layout(node_info, count):
    """(parents, chains) for a glove from SDK node info, falling back to the MANUS default."""
    if node_info and len(node_info) == count:
        index = {node["id"]: i for i, node in enumerate(node_info)}
        parents = [index.get(node["parent"], 0) for node in node_info]
        chains = [node["chain"] if isinstance(node["chain"], str) else "hand" for node in node_info]
        return parents, chains
    if count == len(DEFAULT_PARENTS):
        return DEFAULT_PARENTS, DEFAULT_CHAINS
    return [0]*count, ["hand"]*count


class Vec3(ctypes.Structure):
    _fields_ = [("x", c_float), ("y", c_float), ("z", c_float)]


class Quat(ctypes.Structure):
    _fields_ = [("w", c_float), ("x", c_float), ("y", c_float), ("z", c_float)]


class Transform(ctypes.Structure):
    _fields_ = [("position", Vec3), ("rotation", Quat), ("scale", Vec3)]


class Stamp(ctypes.Structure):
    _fields_ = [("time", c_uint64)]


class StampInfo(ctypes.Structure):
    _fields_ = [("fraction", c_uint16), ("second", c_uint8), ("minute", c_uint8), ("hour", c_uint8),
                ("day", c_uint8), ("month", c_uint8), ("year", c_uint32), ("timecode", c_bool)]


class Version(ctypes.Structure):
    _fields_ = [("major", c_uint32), ("minor", c_uint32), ("patch", c_uint32),
                ("label", c_char*16), ("sha", c_char*16), ("tag", c_char*16)]


class Host(ctypes.Structure):
    _fields_ = [("hostName", c_char*256), ("ipAddress", c_char*40), ("manusCoreVersion", Version)]


class ManusVersion(ctypes.Structure):
    _fields_ = [("versionInfo", c_char*16)]


class CoordinateSystemVUH(ctypes.Structure):
    _fields_ = [("view", c_int), ("up", c_int), ("handedness", c_int), ("unitScale", c_float)]


class RawDeviceData(ctypes.Structure):
    _fields_ = [("id", c_uint32), ("sensorCount", c_uint32), ("sensorData", Transform*5), ("rotation", Quat)]


class RawDeviceDataInfo(ctypes.Structure):
    _fields_ = [("publishTime", Stamp), ("rawDeviceDataCount", c_uint32)]


class ErgonomicsData(ctypes.Structure):
    _fields_ = [("id", c_uint32), ("isUserID", c_bool), ("data", c_float*40)]


class ErgonomicsStream(ctypes.Structure):
    _fields_ = [("publishTime", Stamp), ("data", ErgonomicsData*32), ("dataCount", c_uint32)]


class SkeletonStreamInfo(ctypes.Structure):
    _fields_ = [("publishTime", Stamp), ("skeletonsCount", c_uint32)]


class RawSkeletonInfo(ctypes.Structure):
    _fields_ = [("gloveId", c_uint32), ("nodesCount", c_uint32), ("publishTime", Stamp)]


class SkeletonNode(ctypes.Structure):
    _fields_ = [("id", c_uint32), ("transform", Transform)]


class NodeInfo(ctypes.Structure):
    _fields_ = [("nodeId", c_uint32), ("parentId", c_uint32), ("chainType", c_int),
                ("side", c_int), ("fingerJointType", c_int)]


RawCallback = ctypes.CFUNCTYPE(None, POINTER(RawDeviceDataInfo))
ErgonomicsCallback = ctypes.CFUNCTYPE(None, POINTER(ErgonomicsStream))
SkeletonCallback = ctypes.CFUNCTYPE(None, POINTER(SkeletonStreamInfo))


def _transform(t):
    return [round(t.position.x, 6), round(t.position.y, 6), round(t.position.z, 6),
            round(t.rotation.w, 6), round(t.rotation.x, 6), round(t.rotation.y, 6), round(t.rotation.z, 6)]


class ManusGlove:
    def __init__(self, sdk_dir=None):
        self.sdk_dir = Path(sdk_dir or SDK_DIR)
        self.lock = threading.Lock()
        self.thread = None
        self.stop_event = threading.Event()
        self.dll = None
        self._callbacks = ()
        self._dll_cookie = None
        self.state, self.error, self.core_version = "꺼짐", None, ""
        self.connected = False
        self.arrivals = {}            # glove id -> deque of arrival monotonic times (raw stream)
        self.last_callback = {}       # stream -> monotonic of last callback
        self.last_advance = {}        # stream -> (publish raw value, monotonic when it last changed)
        self.sides, self.nodes = {}, {}
        self.latest_skeleton = {}     # glove id -> (monotonic, [[id, px, py, pz, qw, qx, qy, qz], ...])
        self._pending_ids = set()
        self.writer = None
        self.recording_path = None

    @property
    def available(self):
        return (self.sdk_dir / "ManusSDK.dll").is_file()

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        if self.running or not self.available:
            if not self.available:
                self.state, self.error = "SDK 없음", f"ManusSDK.dll을 찾을 수 없습니다: {self.sdk_dir}"
            return
        self.stop_event.clear()
        self.state, self.error = "연결 중", None
        self.thread = threading.Thread(target=self._run, daemon=True, name="MANUS-client")
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(8)

    def _run(self):
        try:
            self._connect()
            self.connected, self.state = True, "연결됨"
            while not self.stop_event.wait(.5):
                self._resolve_nodes()
        except Exception as error:
            logging.exception("MANUS connection failed")
            self.error, self.state = str(error), "연결 실패"
        finally:
            self.connected = False
            if self.writer:
                self.end()
            if self.dll is not None:
                try:
                    self.dll.CoreSdk_ShutDown()
                except Exception:
                    logging.exception("MANUS shutdown failed")
            self.dll, self._callbacks = None, ()
            if self._dll_cookie is not None:
                self._dll_cookie.close()
                self._dll_cookie = None
            if self.state == "연결됨":
                self.state = "꺼짐"

    def _check(self, code, name):
        if code != SDK_SUCCESS:
            raise RuntimeError(f"{name} 실패 (SDKReturnCode {code})")

    def _connect(self):
        self._dll_cookie = os.add_dll_directory(str(self.sdk_dir))
        dll = ctypes.CDLL(str(self.sdk_dir / "ManusSDK.dll"))
        dll.CoreSdk_GetTimestampInfo.argtypes = [Stamp, POINTER(StampInfo)]
        dll.CoreSdk_GetRawDeviceData.argtypes = [c_uint32, POINTER(RawDeviceData)]
        dll.CoreSdk_GetRawSkeletonInfo.argtypes = [c_uint32, POINTER(RawSkeletonInfo)]
        dll.CoreSdk_GetRawSkeletonData.argtypes = [c_uint32, POINTER(SkeletonNode), c_uint32]
        dll.CoreSdk_GetRawSkeletonNodeCount.argtypes = [c_uint32, POINTER(c_uint32)]
        dll.CoreSdk_GetRawSkeletonNodeInfoArray.argtypes = [c_uint32, POINTER(NodeInfo), c_uint32]
        dll.CoreSdk_InitializeCoordinateSystemWithVUH.argtypes = [CoordinateSystemVUH, c_bool]
        dll.CoordinateSystemVUH_Init.argtypes = [POINTER(CoordinateSystemVUH)]
        dll.CoreSdk_LookForHosts.argtypes = [c_uint32, c_bool]
        dll.CoreSdk_GetNumberOfAvailableHostsFound.argtypes = [POINTER(c_uint32)]
        dll.CoreSdk_GetAvailableHostsFound.argtypes = [POINTER(Host), c_uint32]
        dll.CoreSdk_ConnectToHost.argtypes = [Host]
        dll.CoreSdk_SetRawSkeletonHandMotion.argtypes = [c_int]
        self.dll = dll
        self._callbacks = (RawCallback(self._on_raw), ErgonomicsCallback(self._on_ergonomics),
                           SkeletonCallback(self._on_skeleton))
        self._check(dll.CoreSdk_InitializeCore(), "CoreSdk_InitializeCore")
        self._check(dll.CoreSdk_SetSessionType(SESSION_TYPE_CORE_SDK), "CoreSdk_SetSessionType")
        self._check(dll.CoreSdk_RegisterCallbackForRawDeviceDataStream(self._callbacks[0]), "RawDeviceData 등록")
        self._check(dll.CoreSdk_RegisterCallbackForErgonomicsStream(self._callbacks[1]), "Ergonomics 등록")
        self._check(dll.CoreSdk_RegisterCallbackForRawSkeletonStream(self._callbacks[2]), "RawSkeleton 등록")
        coordinates = CoordinateSystemVUH()
        dll.CoordinateSystemVUH_Init(byref(coordinates))
        coordinates.view, coordinates.up, coordinates.handedness, coordinates.unitScale = 3, 6, 2, 1.0
        self._check(dll.CoreSdk_InitializeCoordinateSystemWithVUH(coordinates, True), "좌표계 설정")
        self._check(dll.CoreSdk_LookForHosts(2, True), "MANUS Core 검색")
        count = c_uint32()
        self._check(dll.CoreSdk_GetNumberOfAvailableHostsFound(byref(count)), "MANUS Core 검색")
        if not count.value:
            raise RuntimeError("실행 중인 MANUS Core를 찾지 못했습니다.")
        hosts = (Host*count.value)()
        self._check(dll.CoreSdk_GetAvailableHostsFound(hosts, count.value), "MANUS Core 검색")
        self._check(dll.CoreSdk_ConnectToHost(hosts[0]), "MANUS Core 연결")
        version = hosts[0].manusCoreVersion
        self.core_version = f"{version.major}.{version.minor}.{version.patch}"
        dll.CoreSdk_SetRawSkeletonHandMotion(HAND_MOTION_AUTO)

    def _resolve_nodes(self):
        with self.lock:
            pending = list(self._pending_ids)
            self._pending_ids.clear()
        for glove_id in pending:
            try:
                count = c_uint32()
                if self.dll.CoreSdk_GetRawSkeletonNodeCount(glove_id, byref(count)) != SDK_SUCCESS or not count.value:
                    raise RuntimeError("node count unavailable")
                nodes = (NodeInfo*count.value)()
                if self.dll.CoreSdk_GetRawSkeletonNodeInfoArray(glove_id, nodes, count.value) != SDK_SUCCESS:
                    raise RuntimeError("node info unavailable")
                info = [{"id": n.nodeId, "parent": n.parentId, "chain": CHAIN_TYPES.get(n.chainType, n.chainType),
                         "joint": JOINT_TYPES.get(n.fingerJointType, n.fingerJointType),
                         "side": SIDE_NAMES.get(n.side, n.side)} for n in nodes]
                sides = [n["side"] for n in info if n["side"] in ("left", "right")]
                with self.lock:
                    self.nodes[glove_id] = info
                    if sides:
                        self.sides[glove_id] = max(set(sides), key=sides.count)
            except Exception:
                logging.info("MANUS node info for %s not ready yet", glove_id)
                with self.lock:
                    if glove_id not in self.nodes:
                        self._pending_ids.add(glove_id)

    # ------------------------------------------------------------------ callbacks (SDK threads)
    def _publish_ms(self, stamp):
        info = StampInfo()
        if self.dll is None or self.dll.CoreSdk_GetTimestampInfo(stamp, byref(info)) != SDK_SUCCESS or not info.year:
            return None
        try:
            return int(datetime(info.year, info.month, info.day, info.hour, info.minute, info.second,
                                info.fraction*1000, tzinfo=timezone.utc).timestamp()*1000)
        except ValueError:
            return None

    def _mark(self, stream, publish_raw, now):
        self.last_callback[stream] = now
        previous = self.last_advance.get(stream)
        if previous is None or previous[0] != publish_raw:
            self.last_advance[stream] = (publish_raw, now)

    def _emit(self, record):
        writer = self.writer
        if writer is not None:
            record["t_host_s"] = round(record.pop("_now") - writer.start, 6)
            writer.put(record)

    def _on_raw(self, pointer):
        try:
            now = time.monotonic()
            info = pointer.contents
            gloves = []
            for index in range(info.rawDeviceDataCount):
                data = RawDeviceData()
                if self.dll.CoreSdk_GetRawDeviceData(index, byref(data)) != SDK_SUCCESS:
                    continue
                gloves.append({"id": data.id, "imu": [round(data.rotation.w, 6), round(data.rotation.x, 6),
                                                       round(data.rotation.y, 6), round(data.rotation.z, 6)],
                               "sensors": [_transform(data.sensorData[k]) for k in range(min(5, data.sensorCount))]})
            with self.lock:
                self._mark("raw", info.publishTime.time, now)
                for glove in gloves:
                    arrivals = self.arrivals.setdefault(glove["id"], deque(maxlen=400))
                    arrivals.append(now)
            if self.writer is not None and gloves:
                self._emit({"stream": "raw", "_now": now, "publish_utc_ms": self._publish_ms(info.publishTime),
                            "gloves": gloves})
        except Exception:
            logging.exception("MANUS raw callback failed")

    def _on_ergonomics(self, pointer):
        try:
            now = time.monotonic()
            stream = pointer.contents
            gloves = []
            for index in range(min(stream.dataCount, 32)):
                row = stream.data[index]
                if row.isUserID:
                    continue
                values = [round(float(v), 4) for v in row.data]
                gloves.append({"id": row.id, "values": values})
            with self.lock:
                self._mark("ergonomics", stream.publishTime.time, now)
                for glove in gloves:
                    if glove["id"] not in self.sides:
                        left, right = (sum(abs(v) for v in glove["values"][:20]),
                                       sum(abs(v) for v in glove["values"][20:]))
                        if max(left, right) > 1:
                            self.sides.setdefault(glove["id"], "left" if left > right else "right")
            if self.writer is not None and gloves:
                self._emit({"stream": "ergonomics", "_now": now,
                            "publish_utc_ms": self._publish_ms(stream.publishTime), "gloves": gloves})
        except Exception:
            logging.exception("MANUS ergonomics callback failed")

    def _on_skeleton(self, pointer):
        try:
            now = time.monotonic()
            stream = pointer.contents
            gloves = []
            for index in range(stream.skeletonsCount):
                info = RawSkeletonInfo()
                if self.dll.CoreSdk_GetRawSkeletonInfo(index, byref(info)) != SDK_SUCCESS:
                    continue
                count = min(int(info.nodesCount), MAX_SKELETON_NODES)
                nodes = (SkeletonNode*count)()
                if self.dll.CoreSdk_GetRawSkeletonData(index, nodes, count) != SDK_SUCCESS:
                    continue
                gloves.append({"id": info.gloveId, "nodes": [[node.id, *_transform(node.transform)] for node in nodes]})
            with self.lock:
                self._mark("skeleton", stream.publishTime.time, now)
                for glove in gloves:
                    self.latest_skeleton[glove["id"]] = (now, glove["nodes"])
                    if glove["id"] not in self.nodes:
                        self._pending_ids.add(glove["id"])
            if self.writer is not None and gloves:
                self._emit({"stream": "skeleton", "_now": now,
                            "publish_utc_ms": self._publish_ms(stream.publishTime), "gloves": gloves})
        except Exception:
            logging.exception("MANUS skeleton callback failed")

    # ------------------------------------------------------------------ status and recording
    def status(self):
        now = time.monotonic()
        with self.lock:
            receiving = any(now - t < 1.5 for t in self.last_callback.values())
            advancing = any(now - t < 1.0 for _, t in self.last_advance.values())
            gloves = {}
            for glove_id, arrivals in self.arrivals.items():
                recent = [t for t in arrivals if now - t < 2]
                gloves[glove_id] = {"side": self.sides.get(glove_id), "hz": len(recent)/2}
        live = self.connected and receiving and advancing
        if not self.connected:
            message = self.error or self.state
        elif not receiving:
            message = "MANUS Core에서 데이터가 오지 않습니다 · 글러브 전원/동글 확인"
        elif not advancing:
            message = "MANUS 대시보드가 녹화 재생 모드입니다 · 대시보드에서 재생을 해제하세요"
        else:
            message = "실시간 수신 중"
        return {"state": self.state, "connected": self.connected, "live": live, "gloves": gloves,
                "message": message, "core_version": self.core_version}

    def hands(self, max_age=1.0):
        """Latest live skeleton per side: {side: {"points": (N,3) wrist-relative m, "parents", "chains", "age"}}."""
        now = time.monotonic()
        with self.lock:
            latest = dict(self.latest_skeleton)
            sides, nodes = dict(self.sides), dict(self.nodes)
        result = {}
        for glove_id, (stamp, data) in latest.items():
            if now - stamp > max_age:
                continue
            points = [row[1:4] for row in data]
            parents, chains = skeleton_layout(nodes.get(glove_id), len(points))
            side = sides.get(glove_id) or f"glove {glove_id}"
            result[side] = {"points": points, "parents": parents, "chains": chains, "age": now - stamp}
        return result

    def begin(self, folder, start_monotonic):
        status = self.status()
        if not status["live"]:
            raise RuntimeError(f"MANUS 글러브 실시간 데이터 없음: {status['message']}")
        with self.lock:
            sides = {str(k): v for k, v in self.sides.items()}
            nodes = {str(k): v for k, v in self.nodes.items()}
        header = {"format_version": 1, "start_monotonic_s": start_monotonic,
                  "started_at": datetime.now().astimezone().isoformat(), "core_version": self.core_version,
                  "time_fields": {"t_host_s": "time.monotonic() - start_monotonic_s; same clock as RGB-D tracks",
                                  "publish_utc_ms": "MANUS Core publish time, UTC epoch ms (1 ms resolution)"},
                  "coordinate_system": "SDK VUH: X from viewer, Z up, right-handed, meters, world coordinates",
                  "glove_sides": sides, "skeleton_nodes": nodes, "ergonomics_names": ERGONOMICS_NAMES,
                  "ergonomics_units": "degrees",
                  "raw_sensor_layout": "[px, py, pz, qw, qx, qy, qz] per sensor; imu = [qw, qx, qy, qz]",
                  "skeleton_node_layout": "[node_id, px, py, pz, qw, qx, qy, qz]"}
        self.recording_path = Path(folder) / "manus.jsonl"
        self.writer = JsonlWriter(self.recording_path, start_monotonic, header, name="MANUS")

    def end(self):
        writer, self.writer = self.writer, None
        if writer is None:
            return None
        writer.close()
        with self.lock:
            sides = {str(k): v for k, v in self.sides.items()}
        return {"file": writer.path.name, "records": writer.counts, "dropped_records": writer.dropped,
                "first_t_host_s": writer.first, "last_t_host_s": writer.last, "glove_sides": sides,
                "core_version": self.core_version, "error": writer.error}


def load_manus(folder, streams=("raw", "ergonomics", "skeleton")):
    """Read ``manus.jsonl`` into per-glove NumPy arrays keyed by stream and glove id."""
    import numpy as np
    path = Path(folder) / "manus.jsonl"
    header, rows = None, {stream: {} for stream in streams}
    markers = tuple(f'"stream":"{stream}"' for stream in streams)
    with path.open(encoding="utf-8") as source:
        for line in source:
            if header is not None and not any(marker in line[:40] for marker in markers):
                continue  # skip unrequested streams without parsing them
            record = json.loads(line)
            if record.get("type") == "header":
                header = record
                continue
            if record.get("stream") not in rows:
                continue
            for glove in record["gloves"]:
                rows[record["stream"]].setdefault(glove["id"], []).append((record["t_host_s"], record["publish_utc_ms"], glove))
    result = {"header": header}
    for stream, gloves in rows.items():
        result[stream] = {}
        for glove_id, samples in gloves.items():
            entry = {"t_host_s": np.array([s[0] for s in samples]),
                     "publish_utc_ms": np.array([s[1] if s[1] is not None else -1 for s in samples], dtype=np.int64),
                     "side": (header or {}).get("glove_sides", {}).get(str(glove_id))}
            if stream == "raw":
                entry["imu"] = np.array([s[2]["imu"] for s in samples], dtype=np.float32)
                entry["sensors"] = np.array([s[2]["sensors"] for s in samples], dtype=np.float32)
            elif stream == "ergonomics":
                entry["values"] = np.array([s[2]["values"] for s in samples], dtype=np.float32)
            else:
                entry["nodes"] = np.array([s[2]["nodes"] for s in samples], dtype=np.float32)
            result[stream][glove_id] = entry
    return result
