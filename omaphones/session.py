"""Execute effects with injected transport, clock and output; also used in replay."""
from omaphones.api import Event, Send, Schedule, Report, Finish, CancelTimer, NextEndpoint
from omaphones.state import State


class Session:
    def __init__(self, adapter, transport, clock, output, ended=lambda code, message: None, limits=None, recorder=None, observer=None):
        self.adapter = adapter
        self.transport = transport
        self.clock = clock
        self.output = output
        self.ended = ended
        self.state = State(limits)
        self.recorder = recorder
        self.observer = observer
        self.exit_code = None
        self.message = ""
        self.timers = {}

    def dispatch(self, event):
        if self.exit_code is not None:
            return
        if event.kind == "timer":
            self.timers.pop(event.value, None)
        try:
            if self.recorder:
                self.recorder.event(event)
            if event.kind == "command" and not self.state.accepts(event.control, event.value):
                return
            effects = self.adapter.on_event(event)
            for effect in effects:
                if self.exit_code is not None:
                    break
                if isinstance(effect, Send):
                    self.transport.write(effect.data)
                elif isinstance(effect, NextEndpoint):
                    self.transport.next_endpoint()
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
                    if event.kind == 'command':
                        raise ValueError('a command cannot report device state before RX')
                    changed = self.state.apply(effect)
                    if changed and self.recorder:
                        self.recorder.record('state', self.state.snapshot())
                    if self.observer:
                        self.observer(self.state, self.state.observed if event.kind == 'received' else [], changed)
                    elif changed:
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
        try:
            for token in self.timers.values():
                self.clock.cancel(token)
            self.timers.clear()
            self.adapter.on_event(Event("stop"))
        except Exception as error:
            if self.exit_code == 0:
                self.exit_code = 1
            self.message = (self.message + '; ' if self.message else '') + 'adapter cleanup: ' + str(error)
        finally:
            try:
                self.transport.close()
            finally:
                self.ended(self.exit_code, self.message)
