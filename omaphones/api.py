"""The entire adapter boundary: events in, effects out. No platform imports."""
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Event:
    kind: str
    value: Any = None
    control: str = ""


@dataclass(frozen=True)
class Send:
    data: bytes


@dataclass(frozen=True)
class Schedule:
    token: int
    milliseconds: int


@dataclass(frozen=True)
class CancelTimer:
    token: int


@dataclass(frozen=True)
class Report:
    values: dict
    capabilities: dict = field(default_factory=dict)
    observed: tuple | None = None


@dataclass(frozen=True)
class Finish:
    code: int
    message: str = ""


@dataclass(frozen=True)
class NextEndpoint:
    """Protocol rejected a connected candidate; host tries the next declared one."""
    pass


class Protocol:
    """A private conversation per device. Helpers only accumulate effects.

    Implement connected(), received(bytes), command(control, value). Timers
    name protocol methods; only the host measures time and invokes them.
    A stopped conversation cannot send, report or schedule further work.
    """

    def __init__(self, model=None):
        self.model = dict(model or {})
        self.exit_code = None
        self.effects = []
        self._timers = {}
        self._serial = 0

    def on_event(self, event):
        self.effects = []
        if self.exit_code is not None:
            return []
        if event.kind == "connected":
            self.connected()
        elif event.kind == "received":
            self.received(event.value)
        elif event.kind == "command":
            self.command(event.control, event.value)
        elif event.kind == "timer":
            callback = self._timers.pop(event.value, None)
            if callback:
                method, args = callback
                getattr(self, method)(*args)
        elif event.kind == "disconnected":
            self.finish(1, event.value or "the device link closed")
        elif event.kind == "stop":
            self.finish(0)
        else:
            raise ValueError("unknown adapter event: " + event.kind)
        return list(self.effects)

    def write(self, data):
        if self.exit_code is None:
            self.effects.append(Send(bytes(data)))

    def next_endpoint(self):
        if self.exit_code is None:
            self.effects.append(NextEndpoint())

    def schedule(self, milliseconds, method, *args):
        if self.exit_code is not None:
            return
        self._serial += 1
        self._timers[self._serial] = (method, args)
        self.effects.append(Schedule(self._serial, milliseconds))
        return self._serial

    def cancel_timer(self, token):
        if self.exit_code is None and token in self._timers:
            self._timers.pop(token)
            self.effects.append(CancelTimer(token))

    def report(self, values, capabilities=None, observed=None):
        if self.exit_code is None:
            self.effects.append(Report(values, capabilities or {}, None if observed is None else tuple(observed)))

    def finish(self, code, message=""):
        if self.exit_code is None:
            self.exit_code = code
            self._timers.clear()
            self.effects.append(Finish(code, message))
        return False

    def connected(self):
        pass

    def received(self, data):
        raise NotImplementedError

    def command(self, control, value):
        pass
