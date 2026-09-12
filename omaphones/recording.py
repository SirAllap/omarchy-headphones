"""Host-owned receipt log: actual RX, successful TX, inputs, timers and state."""
import json
from pathlib import Path
import threading
import time
import os


def safe_output(path):
    installed = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'omarchy/plugins/io.github.ncr.omaphones'
    if path.resolve().is_relative_to(installed.resolve()):
        raise ValueError('capture output must not reload the running plugin')


class Recorder:
    def __init__(self, path, metadata):
        safe_output(path)
        self.file = path.open('x')
        self.lock = threading.Lock()
        self.serial = 0
        self.start = time.monotonic_ns()
        self.record('metadata', metadata)

    def record(self, direction, value):
        with self.lock:
            self.serial += 1
            event = {'id': str(self.serial), 'timeNs': time.monotonic_ns() - self.start, 'direction': direction}
            if isinstance(value, bytes):
                event.update(encoding='hex', data=value.hex(' '))
            else:
                event.update(encoding='json', data=value)
            self.file.write(json.dumps(event, separators=(',', ':')) + '\n')
            self.file.flush()

    def event(self, event):
        if event.kind == 'received':
            self.record('rx', event.value)
        elif event.kind == 'command':
            self.record('command', {'control': event.control, 'value': event.value})
        elif event.kind == 'timer':
            self.record('timer', event.value)
        else:
            self.record('event', {'kind': event.kind, 'value': event.value})

    def close(self):
        self.file.close()
