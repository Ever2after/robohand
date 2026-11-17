"""External sensors recorded alongside the cameras: MANUS gloves and VIVE trackers.

Each device class exposes ``start/stop``, ``status()`` (with a ``live`` flag),
``begin(folder, start_monotonic)`` and ``end() -> summary`` so the camera Recorder can
record it into the same session on the same monotonic clock.
"""
