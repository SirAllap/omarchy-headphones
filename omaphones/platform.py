"""Process lifetime and GLib clock. Loaded only by the device runner."""
import ctypes
import json
import os
import signal
import sys


def arm_parent_death_signal():
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)
    except Exception:
        return
    if os.getppid() == 1:
        os._exit(0)


def emit(payload):
    try:
        sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    except (BrokenPipeError, ValueError):
        raise SystemExit(0)


class GLibClock:
    def __init__(self, glib):
        self.glib = glib

    def schedule(self, ms, callback):
        def run():
            callback()
            return False
        return self.glib.timeout_add(ms, run)

    def cancel(self, token):
        self.glib.source_remove(token)
