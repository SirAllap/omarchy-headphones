"""Deterministic API replay tools. No radio, files, real clock or subprocesses."""
from omaphones.api import Event
from omaphones.session import Session


class Clock:
    def __init__(self):
        self.now = 0
        self.serial = 0
        self.pending = {}

    def schedule(self, ms, callback):
        self.serial += 1
        self.pending[self.serial] = (self.now + ms, callback)
        return self.serial

    def cancel(self, token):
        self.pending.pop(token, None)

    def advance(self, milliseconds):
        if type(milliseconds) is not int or milliseconds < 0:
            raise ValueError("advance needs a nonnegative integer")
        end = self.now + milliseconds
        count = 0
        while self.pending:
            token, (at, callback) = min(self.pending.items(), key=lambda item: (item[1][0], item[0]))
            if at > end:
                break
            count += 1
            if count > 10000:
                raise AssertionError("timer loop exceeds replay budget")
            self.pending.pop(token)
            self.now = at
            callback()
        self.now = end


class Replay:
    """Drive the same Session as the live runner; all effects are inspectable."""
    def __init__(self, adapter):
        self.clock = Clock()
        self.sent = []
        self.reports = []
        self.closed = False
        self.session = Session(adapter, self, self.clock, lambda state: self.reports.append(state.snapshot()))

    def write(self, data):
        if self.closed:
            raise AssertionError("write after close")
        self.sent.append(bytes(data).hex(' '))

    def close(self):
        self.closed = True

    def event(self, kind, value=None, control=""):
        self.session.dispatch(Event(kind, value, control))

    def receive(self, data):
        self.event("received", bytes.fromhex(data) if isinstance(data, str) else data)

    def command(self, control, value):
        self.event("command", value, control)

    def advance(self, milliseconds):
        self.clock.advance(milliseconds)

    def play(self, test, pin):
        for index, step in enumerate(pin["steps"]):
            keys = [key for key in step if key != "note"]
            if len(keys) != 1:
                raise ValueError("one action or assertion per pin step")
            key = keys[0]; value = step[key]
            where = "step %d: %s" % (index, key)
            if key == "event":
                self.event(value)
            elif key == "device":
                self.receive(value)
            elif key == "command":
                self.command(value["control"], value["value"])
            elif key == "advance":
                self.advance(value)
            elif key == "sent":
                test.assertEqual(self.sent, value, where)
            elif key == "reports":
                test.assertEqual(self.reports, value, where)
            elif key == "values":
                test.assertEqual(self.session.state.values, value, where)
            elif key == "exit":
                test.assertEqual(self.session.exit_code, value, where)
            else:
                raise ValueError("unknown API pin step: " + key)
