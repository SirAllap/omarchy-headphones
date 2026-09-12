"""Replay observed transport input with independently reviewed expectations.

The existing owners' harness and pins are not changed. V1 packages use raw
capture references and always compare complete outgoing sequences and state.
"""
import copy
import json
import tempfile
import threading
import unittest

from . import profiles, runtime


def capture(path, profile):
    events = {}
    last_time = -1
    for number, line in enumerate(path.read_text().splitlines(), 1):
        event = json.loads(line)
        if not isinstance(event, dict) or not isinstance(event.get("id"), str) or event["id"] in events:
            raise ValueError("capture line %d: missing or duplicate id" % number)
        if type(event.get("timeNs")) is not int or event["timeNs"] < last_time:
            raise ValueError("capture timestamps must be monotonic receipt times")
        last_time = event["timeNs"]
        direction, encoding = event.get("direction"), event.get("encoding")
        if direction not in ("metadata", "rx", "tx", "command", "state", "external"):
            raise ValueError("unknown capture direction")
        if encoding not in ("hex", "text", "json"):
            raise ValueError("unknown capture encoding")
        if encoding == "hex":
            if not isinstance(event.get("data"), str):
                raise ValueError("hex capture data must be a string")
            bytes.fromhex(event["data"])
        if direction in ("rx", "tx") and encoding not in ("hex", "text"):
            raise ValueError("transport data must be bytes or a client line")
        events[event["id"]] = event
    first = next(iter(events.values()), {})
    metadata = first.get("data", {})
    if (first.get("direction") != "metadata" or not isinstance(metadata, dict)
            or metadata.get("apiVersion") != 1 or metadata.get("device") != profile["id"]):
        raise ValueError("capture must begin with metadata for this device")
    if not profiles.match_profile(profile, metadata.get("name", ""), metadata.get("uuids", []), metadata.get("modelId", "")):
        raise ValueError("capture identity does not match profile")
    if metadata.get("synthetic"):
        raise ValueError("synthetic capture cannot be owner evidence")
    return events


def data(event):
    return bytes.fromhex(event["data"]) if event["encoding"] == "hex" else event["data"]


class BuiltinSession:
    """Use each existing test's transport double, with the runtime configuration.

    Raw RX is fed directly to the production framer, never re-encoded by it.
    TX includes Sony ACKs too. JBL's explicitly labelled boundary is client lines.
    """
    def __init__(self, context):
        from tests import harness
        self.name = context.profile["adapter"]["builtin"]
        # Fresh test module and bridge: no mutation of the canonical test modules.
        module = runtime.load_module(profiles.ROOT / "tests" / (self.name + "_bridge_test.py"))
        self.module = runtime.configure(module.bridge_module, context)
        if self.name == "sony":
            uuid = next(u for u in profiles.BUILTINS["sony"]["uuids"] if u in context.uuids)
            self.session = module.Session(uuid=uuid, name=context.name)
        elif self.name == "soundcore":
            uuid = next(u for u in context.uuids if u.startswith(profiles.BUILTINS["soundcore"]["prefix"]))
            self.session = module.Session(uuid=uuid)
        else:
            self.session = module.Session()
        self.bridge = self.session.bridge
        context.output = self.session.lines.append
        self.module.emit = context.emit
        self.glib = harness.FakeGLib()
        if self.name != "jbl":
            self.module.GLib = self.glib

    @property
    def sent(self):
        return list(self.session.frames)

    @property
    def lines(self):
        return self.session.lines

    def start(self):
        if self.name == "jbl":
            self.session.do_start()
        elif self.name == "nothing":
            self.session.do_open()
        else:
            self.bridge.fd = None
            self.bridge.opened(-1)

    def receive(self, raw):
        if self.name == "jbl":
            if not isinstance(raw, str):
                raise ValueError("JBL replay expects client lines")
            self.session.device(raw)
            return
        if not isinstance(raw, bytes):
            raise ValueError("stream replay expects raw bytes")
        self.bridge.buffer += raw
        if self.name in ("sony", "samsung", "soundcore"):
            self.bridge.parse_buffer()
        elif self.name == "nothing":
            for frame in self.module.take_frames(self.bridge.buffer):
                self.bridge.on_frame(*frame)
        else:
            frames, self.bridge.buffer = self.module.take_frames(self.bridge.buffer)
            for frame in frames:
                self.bridge.on_frame(*frame)

    def command(self, line):
        self.session.command(line)

    def advance(self, milliseconds=None):
        if milliseconds is not None:
            raise ValueError("GLib replay advances timer batches with true; numeric time is for socket run loops")
        pending, self.glib.timers = self.glib.timers, []
        for _ms, fn, args, _serial in pending:
            fn(*args)

    def wait_sent(self, count):
        if self.name == "jbl":
            self.session.wait_sent(count)

    def close(self):
        if hasattr(self.session, "close"):
            self.session.close()


class ClockedLoopSession:
    """Run the actual Nothing/Bose loop against a deterministic clock and socket.

    Input and elapsed time wake select/recv; each action waits until the loop is
    blocked again. No manual poll or finish substitutes for protocol deadlines.
    Bose's real init validator runs before its normal loop, retaining joined RX.
    """
    def __init__(self, context):
        from tests import harness
        self.name = context.profile["adapter"]["builtin"]
        self.lines, self.sent = [], []
        self.now = 0.0
        self.condition = threading.Condition()
        self.pending = []
        self.waiting = False
        self.generation = 0
        self.wake = self.closed = self.finished = False
        self.error = None
        self.thread = None
        self.tmp = tempfile.TemporaryDirectory(prefix="omaphones-replay-")
        context.output = self.lines.append
        self.module = runtime.configure(harness.load_bridge(self.name + "-bridge"), context)
        self.module.time = runtime.Proxy(self.module.time, monotonic=lambda: self.now)
        self.module.select = runtime.Proxy(self.module.select, select=lambda *_args: (self.block(), [], []))
        if self.name == "nothing":
            self.module.STATE_DIR = self.tmp.name
            self.module.CASE_FILE = self.tmp.name + "/nothing-case.json"
            self.bridge = self.module.Bridge(context.address, context.name)
        else:
            self.bridge = self.module.Bridge(context.address)
        outer = self

        class Socket:
            def sendall(self, raw):
                outer.sent.append(bytes(raw))

            def recv(self, _count):
                with outer.condition:
                    if not outer.pending and not outer.closed:
                        outer.block()
                    if outer.pending:
                        return outer.pending.pop(0)
                    if outer.closed:
                        return b""
                    raise outer.module.socket.timeout()

            def settimeout(self, _value):
                pass

            def close(self):
                with outer.condition:
                    outer.closed = True
                    outer.condition.notify_all()

        self.socket = Socket()
        self.bridge.sock = self.socket
        self.bridge.connect = (lambda: True) if self.name == "nothing" else (lambda: self.bridge._speaks_bmap(self.socket))
        self.session = self

    def block(self):
        with self.condition:
            self.waiting = True
            self.generation += 1
            self.condition.notify_all()
            self.condition.wait_for(lambda: self.pending or self.wake or self.closed)
            self.waiting = False
            self.wake = False
            return [self.socket] if self.pending or self.closed else []

    def settled(self, generation):
        if not self.condition.wait_for(lambda: self.finished or self.waiting and self.generation > generation, timeout=3):
            raise AssertionError("adapter run loop did not settle")
        if self.error:
            raise self.error

    def start(self):
        def run():
            try:
                self.bridge.run()
            except BaseException as error:
                self.error = error
            finally:
                with self.condition:
                    self.finished = True
                    self.condition.notify_all()

        with self.condition:
            self.thread = threading.Thread(target=run, daemon=True)
            self.thread.start()
            self.settled(0)

    def receive(self, raw):
        if not isinstance(raw, bytes):
            raise ValueError("socket replay requires raw bytes")
        with self.condition:
            generation = self.generation
            self.pending.append(raw)
            self.condition.notify_all()
            self.settled(generation)

    def command(self, line):
        self.bridge.command(line)

    def advance(self, milliseconds=None):
        if milliseconds is None:
            deadlines = [getattr(self.bridge, key, None) for key in ("readback_at", "next_poll")]
            if self.name == "nothing":
                deadlines += [self.bridge.opened_at + self.module.INFO_TIMEOUT,
                              self.bridge.opened_at + self.module.INFO_TIMEOUT + self.module.ANSWER_TIMEOUT]
            else:
                deadlines += [self.now + self.module.PROBE_TIMEOUT,
                              (self.bridge.opened_at or 0) + self.module.ANSWER_TIMEOUT]
            future = [d for d in deadlines if d is not None and d > self.now]
            target = min(future) if future else self.now + 1
        else:
            target = self.now + milliseconds / 1000
        with self.condition:
            generation = self.generation
            self.now = target
            self.wake = True
            self.condition.notify_all()
            self.settled(generation)

    def wait_sent(self, _count):
        pass # Every receive/advance already waits for the production loop.

    def close(self):
        self.bridge.finish(0)
        self.socket.close()
        if self.thread:
            self.thread.join(timeout=3)
            if self.thread.is_alive():
                raise AssertionError("adapter run loop did not stop")
        self.tmp.cleanup()


def factory(profile, directory, metadata):
    context = runtime.Context(profile, directory, metadata["address"], metadata["name"], metadata["uuids"],
                              metadata.get("bleAddress", ""), metadata.get("modelId", ""))
    if profile["adapter"].get("builtin"):
        if profile["adapter"]["builtin"] in ("nothing", "bose"):
            return ClockedLoopSession(context)
        return BuiltinSession(context)
    return runtime.local_module(context).replay(context)


def execute(profile, directory, events, session_spec, split=None):
    if session_spec.get("apiVersion") != 1 or not session_spec.get("steps"):
        raise ValueError("session needs apiVersion: 1 and nonempty steps")
    metadata = next(iter(events.values()))["data"]
    session = factory(profile, directory, metadata)
    checker = unittest.TestCase()
    checks = set()
    assertions = {"tx": 0, "state": 0}
    try:
        for index, step in enumerate(session_spec["steps"]):
            where = "step %d" % (index + 1)
            if not isinstance(step, dict):
                raise ValueError(where + ": expected an object")
            keys = set(step) - {"note", "case"}
            if len(keys) != 1:
                raise ValueError(where + ": use exactly one action or assertion")
            action = next(iter(keys))
            value = step[action]
            if action == "start" and value is True:
                session.start()
            elif action == "rx":
                event = events[value]
                if event["direction"] != "rx":
                    raise ValueError(where + ": RX references must name received data")
                raw = data(event)
                if split and split[0] == index and isinstance(raw, bytes):
                    boundary = split[1]
                    session.receive(raw[:boundary])
                    session.receive(raw[boundary:])
                else:
                    session.receive(raw)
            elif action == "command":
                if not isinstance(value, str):
                    raise ValueError("command must be a string")
                before = copy.deepcopy(session.lines)
                session.command(value)
                # A requested setting is never a reported setting.
                checker.assertEqual(session.lines, before, where + ": command changed reported state without RX")
            elif action == "advance" and (value is True or type(value) is int and value > 0):
                session.advance(None if value is True else value)
            elif action == "tx":
                if not isinstance(value, list) or not value:
                    raise ValueError(where + ": tx needs a nonempty list of captured TX ids")
                wanted = []
                for ref in value:
                    if events[ref]["direction"] != "tx":
                        raise ValueError(where + ": expected a TX capture reference")
                    wanted.append(data(events[ref]))
                session.wait_sent(len(wanted))
                actual = session.sent
                if all(isinstance(item, bytes) for item in wanted + actual):
                    # Partial successful writes are transport chunks, not packet boundaries.
                    checker.assertEqual(b"".join(actual), b"".join(wanted), where + ": outgoing bytes changed")
                else:
                    checker.assertEqual(actual, wanted, where + ": complete outgoing sequence changed")
                assertions["tx"] += 1
            elif action == "expect":
                if not isinstance(value, dict) or not value:
                    raise ValueError(where + ": fill the independently reviewed expected state")
                checker.assertTrue(session.lines, where + ": no reported state")
                checker.assertEqual(session.lines[-1], value, where)
                assertions["state"] += 1
                if step.get("case"):
                    label = step["case"]
                    validate_case(label, value)
                    checks.add(label)
            else:
                raise ValueError(where + ": unknown or incomplete session step " + action)
        if not all(assertions.values()):
            raise ValueError("session must assert captured TX and independently reviewed reported state")
        return checks
    finally:
        session.close()


def validate_case(label, state):
    """A label alone cannot turn an unrelated reply into capability coverage."""
    key, _, value = label.partition(":")
    expected = {"mode": ("mode", value), "ancLevel": ("ancLevel", value),
                "ambient": ("level", int(value) if value.isdigit() else None),
                "voice": ("voice", value == "on"), "latency": ("latency", value == "on"),
                "worn": ("worn", value == "on")}
    if key in expected:
        field, wanted = expected[key]
        if field not in state or state[field] != wanted:
            raise ValueError("case " + label + " does not match the asserted state")
    elif key == "battery":
        reading = state.get("battery", {}).get(value)
        if type(reading) is not int or not 0 <= reading <= 100:
            raise ValueError("case " + label + " needs a reported battery reading")
    elif label not in ("initial", "external-change", "repeated", "unsupported-command"):
        raise ValueError("unknown replay case " + label)


def verify(profile, directory, fragment=True):
    events = capture(directory / "capture.jsonl", profile)
    spec = profiles.read_json(directory / "session.json")
    checks = execute(profile, directory, events, spec)
    if fragment:
        for index, step in enumerate(spec["steps"]):
            if "rx" not in step:
                continue
            raw = data(events[step["rx"]])
            if isinstance(raw, bytes):
                for boundary in range(1, len(raw)):
                    execute(profile, directory, events, spec, split=(index, boundary))
    return checks
