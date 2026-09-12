"""Execute effects with injected transport, clock and output; also used in replay."""
from omaphones.api import Event, Send, Schedule, Report, Finish, CancelTimer
from omaphones.state import State


class Session:
    def __init__(self, adapter, transport, clock, output, ended=lambda code, message: None):
        self.adapter = adapter
        self.transport = transport
        self.clock = clock
        self.output = output
        self.ended = ended
        self.state = State()
        self.exit_code = None
        self.message = ""
        self.timers = {}

    def dispatch(self, event):
        if self.exit_code is not None:
            return
        if event.kind == "command" and not self.state.accepts(event.control, event.value):
            return
        if event.kind == "timer":
            self.timers.pop(event.value, None)
        try:
            effects = self.adapter.on_event(event)
            for effect in effects:
                if self.exit_code is not None:
                    break
                if isinstance(effect, Send):
                    self.transport.write(effect.data)
                elif isinstance(effect, Schedule):
                    if type(effect.milliseconds) is not int or effect.milliseconds < 0:
                        raise ValueError("invalid adapter timer")
                    token = effect.token
                    self.timers[token] = self.clock.schedule(effect.milliseconds, lambda token=token: self.dispatch(Event("timer", token)))
                elif isinstance(effect, CancelTimer):
                    scheduled = self.timers.pop(effect.token, None)
                    if scheduled is not None:
                        self.clock.cancel(scheduled)
                elif isinstance(effect, Report):
                    if self.state.apply(effect):
                        self.output(self.state)
                elif isinstance(effect, Finish):
                    self.finish(effect.code, effect.message)
                else:
                    raise ValueError("unknown adapter effect")
        except Exception as error:
            self.finish(1, "%s: %s" % (type(error).__name__, error))

    def finish(self, code, message=""):
        if self.exit_code is not None:
            return
        self.exit_code, self.message = code, message
        for token in self.timers.values():
            self.clock.cancel(token)
        self.timers.clear()
        self.adapter.on_event(Event("stop"))
        try:
            self.transport.close()
        finally:
            self.ended(code, message)
