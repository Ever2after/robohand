"""Background JSON-lines writer shared by the device recorders."""
from __future__ import annotations

import json
import logging
from pathlib import Path
import queue
import threading


class JsonlWriter:
    """Header line, then one compact JSON record per line; device callbacks never wait on disk.

    Every record must carry ``stream`` and ``t_host_s``; ``start`` is the session's
    start_monotonic_s so producers can compute ``t_host_s`` on the shared clock.
    """

    def __init__(self, path, start_monotonic, header, name="jsonl"):
        self.path, self.start = Path(path), start_monotonic
        self.queue = queue.Queue(maxsize=50000)
        self.dropped = 0
        self.counts = {}
        self.first = self.last = None
        self.error = None
        self.file = self.path.open("w", encoding="utf-8", newline="\n")
        self.file.write(json.dumps({"type": "header", **header}, ensure_ascii=False) + "\n")
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"{name}-writer")
        self.thread.start()

    def put(self, record):
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1

    def _run(self):
        try:
            while True:
                record = self.queue.get()
                if record is None:
                    break
                self.counts[record["stream"]] = self.counts.get(record["stream"], 0) + 1
                self.first = record["t_host_s"] if self.first is None else self.first
                self.last = record["t_host_s"]
                self.file.write(json.dumps(record, separators=(",", ":")) + "\n")
        except Exception as error:
            logging.exception("%s writer failed", self.path.name)
            self.error = str(error)
        finally:
            self.file.close()

    def close(self):
        self.queue.put(None)
        self.thread.join(10)
