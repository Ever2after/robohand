"""Isolate libsurvive and USB access from the local HTTP interface."""

import argparse
import ctypes
import json
import math
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent
# Prebuilt Windows libsurvive (see vendor/libsurvive/README.md); plugins/ must sit next to the DLL.
BUILD = ROOT / "vendor" / "libsurvive" / "bin"
OUTPUT_LOCK = threading.Lock()
DLL_DIRECTORY = None


def emit(event_name, **fields):
    line = json.dumps({"event": event_name, **fields}, ensure_ascii=False, allow_nan=False)
    with OUTPUT_LOCK:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


class Pose(ctypes.Structure):
    _fields_ = [("position", ctypes.c_double * 3), ("quaternion", ctypes.c_double * 4)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", action="store_true")
    parser.add_argument("--calibrate", action="store_true")
    options = parser.parse_args()
    os.chdir(ROOT)
    (ROOT / "runtime").mkdir(exist_ok=True)

    global DLL_DIRECTORY  # the handle must stay alive while libsurvive is loaded
    DLL_DIRECTORY = os.add_dll_directory(str(BUILD))  # also provides libc++/libunwind
    library = ctypes.CDLL(str(BUILD / "libsurvive.dll"))
    pointer = ctypes.c_void_p
    log_callback_type = ctypes.CFUNCTYPE(None, pointer, ctypes.c_int, ctypes.c_char_p)
    signatures = {
        "survive_simple_init_with_logger": ([ctypes.c_int, ctypes.POINTER(ctypes.c_char_p), log_callback_type], pointer),
        "survive_simple_start_thread": ([pointer], None),
        "survive_simple_is_running": ([pointer], ctypes.c_bool),
        "survive_simple_close": ([pointer], None),
        "survive_simple_get_first_object": ([pointer], pointer),
        "survive_simple_get_next_object": ([pointer, pointer], pointer),
        "survive_simple_get_next_updated": ([pointer], pointer),
        "survive_simple_object_name": ([pointer], ctypes.c_char_p),
        "survive_simple_serial_number": ([pointer], ctypes.c_char_p),
        "survive_simple_object_get_type": ([pointer], ctypes.c_int),
        "survive_simple_object_get_latest_pose": ([pointer, ctypes.POINTER(Pose)], ctypes.c_double),
        "studio_object_metrics": ([pointer, pointer, ctypes.POINTER(ctypes.c_double)], ctypes.c_int),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(library, name)
        function.argtypes = arguments
        function.restype = result

    @log_callback_type
    def native_log(context, level, message):
        emit("log", level=level, message=(message or b"").decode("utf-8", errors="replace").strip())

    arguments = ["tracker-studio", "-c", "./runtime/calibration.json", "--lighthouse-gen", "2",
                 "--gss-auto-floor-height", "0", "--v", "1"]
    if options.pair:
        arguments.append("--pair-device")
    if options.calibrate:
        arguments.append("--force-calibrate")
    encoded = [argument.encode("utf-8") for argument in arguments]
    argv = (ctypes.c_char_p * (len(encoded) + 1))(*encoded, None)

    import hid
    receivers = [{"serial": entry["serial_number"], "product": entry["product_string"]}
                 for entry in hid.enumerate(0x28DE, 0x2101)]
    emit("receivers", receivers=receivers)
    context = library.survive_simple_init_with_logger(len(encoded), argv, native_log)
    if not context:
        raise RuntimeError("libsurvive 초기화에 실패했습니다. 진단 로그를 확인하세요.")

    stopped = threading.Event()

    def read_commands():
        for line in sys.stdin:
            if line.strip() == "stop":
                stopped.set()
                return
        stopped.set()

    threading.Thread(target=read_commands, daemon=True).start()
    metadata = {}
    pose_times = {}

    def describe(object_pointer):
        name = (library.survive_simple_object_name(object_pointer) or b"").decode("utf-8", "replace")
        serial = (library.survive_simple_serial_number(object_pointer) or b"").decode("utf-8", "replace")
        object_type = library.survive_simple_object_get_type(object_pointer)
        metrics = (ctypes.c_double * 7)()
        has_metrics = library.studio_object_metrics(context, object_pointer, metrics)
        fields = {"key": name, "serial": serial, "kind": "base" if object_type == 1 else "tracker"}
        if has_metrics:
            fields.update(light_age_s=metrics[0], imu_received=bool(metrics[1]),
                          sensor_count=int(metrics[2]), battery=int(metrics[5]), charging=bool(metrics[6]))
        metadata[object_pointer] = fields
        return fields

    library.survive_simple_start_thread(context)
    emit("ready", pairing=options.pair)
    next_heartbeat = 0
    try:
        while not stopped.is_set() and library.survive_simple_is_running(context):
            now = time.monotonic()
            if now >= next_heartbeat:
                objects = []
                object_pointer = library.survive_simple_get_first_object(context)
                while object_pointer:
                    objects.append(describe(object_pointer))
                    object_pointer = library.survive_simple_get_next_object(context, object_pointer)
                emit("devices", devices=objects)
                next_heartbeat = now + 0.5

            object_pointer = library.survive_simple_get_next_updated(context)
            if not object_pointer:
                time.sleep(0.001)
                continue
            fields = metadata.get(object_pointer) or describe(object_pointer)
            pose = Pose()
            pose_time = library.survive_simple_object_get_latest_pose(object_pointer, ctypes.byref(pose))
            if pose_time <= 0 or pose_times.get(object_pointer) == pose_time:
                continue
            position = list(pose.position)
            quaternion = list(pose.quaternion)
            if not all(math.isfinite(value) for value in position + quaternion):
                continue
            if sum(value * value for value in quaternion) < 0.01:
                continue
            pose_times[object_pointer] = pose_time
            metrics = (ctypes.c_double * 7)()
            if library.studio_object_metrics(context, object_pointer, metrics):
                fields = {**fields, "light_age_s": metrics[0], "imu_received": bool(metrics[1])}
            emit("pose", **fields, position=position, quaternion=quaternion, pose_time_s=pose_time)
    finally:
        library.survive_simple_close(context)
        emit("closed")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        emit("error", message=str(error))
        raise
