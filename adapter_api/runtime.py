"""Launch one model in its own process, using an existing or package-local adapter.

Local adapters export API_VERSION = 1 and run(context). They own their link and
implement BRIDGE.md. context.emit(state) and context.record(direction, data)
are the shared state/capture boundary; replay(context) provides the offline
Session described in docs/ADAPTER-API.md. No adapter is imported by QML.
"""
import ctypes
import importlib.machinery
import importlib.util
import json
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass

from . import profiles


def load_module(path):
    loader = importlib.machinery.SourceFileLoader("omaphones_" + path.stem.replace("-", "_"), str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def arm_parent_death():
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)
    except OSError:
        pass
    if os.getppid() == 1:
        os._exit(0)


def emit(payload):
    try:
        print(json.dumps(payload, separators=(",", ":")), flush=True)
    except (BrokenPipeError, ValueError):
        os._exit(0)


class Recorder:
    """Receipt-time stream chunks, never reconstructed packets or invented timing."""
    def __init__(self, path, metadata):
        self.file = path.open("x")
        self.lock = threading.Lock()
        self.serial = 0
        self.start = time.monotonic_ns()
        self.record("metadata", metadata)

    def record(self, direction, data, endpoint=None):
        with self.lock:
            self.serial += 1
            event = {"id": str(self.serial), "timeNs": time.monotonic_ns() - self.start,
                     "direction": direction}
            if endpoint is not None:
                event["endpoint"] = endpoint
            if isinstance(data, bytes):
                event.update(encoding="hex", data=data.hex(" "))
            elif isinstance(data, str):
                event.update(encoding="text", data=data)
            else:
                event.update(encoding="json", data=data)
            self.file.write(json.dumps(event, separators=(",", ":")) + "\n")
            self.file.flush()

    def close(self):
        self.file.close()


def allowed(profile, line):
    caps = profile["capabilities"]
    words = line.strip().split()
    if len(words) != 2:
        return False
    key, value = words
    if key == "set":
        return value in caps["modes"]
    if key == "level":
        if value in caps.get("ancLevels", []):
            return True
        ambient = caps.get("ambient", {})
        return bool(ambient) and value.isdigit() and ambient["min"] <= int(value) <= ambient["max"]
    if key == "latency":
        return caps.get("latency", False) and value in ("on", "off")
    ambient = caps.get("ambient", {})
    return key == ambient.get("voiceCommand") and value in ("on", "off")


def state_for(profile, state):
    state = dict(state)
    caps = profile["capabilities"]
    if "available" in state or state.get("modes"):
        state["available"] = [m for m in state.get("available", profiles.MODES) if m in caps["modes"]]
    for keys, present in ((["level", "voice"], caps.get("ambient")),
                          (["ancLevel", "ancLevels"], caps.get("ancLevels")),
                          (["latency"], caps.get("latency")), (["worn"], caps.get("worn"))):
        if not present:
            for key in keys:
                state.pop(key, None)
    if "ancLevels" in state:
        state["ancLevels"] = [v for v in state["ancLevels"] if v in caps["ancLevels"]]
    battery = caps.get("battery", {})
    if battery.get("source") != "bridge":
        state.pop("battery", None)
    elif isinstance(state.get("battery"), dict):
        data = state["battery"]
        state["battery"] = {k: v for k, v in data.items() if k in battery["parts"] or k == "caseStale" and "case" in battery["parts"]}
        if "charging" in data:
            state["battery"]["charging"] = [k for k in data["charging"] if k in battery["parts"]]
    return state


@dataclass
class Context:
    profile: dict
    directory: object
    address: str
    name: str
    uuids: list
    ble_address: str = ""
    model_id: str = ""
    recorder: object = None
    output: object = emit
    mode_seen: bool = False

    def record(self, direction, data, endpoint=None):
        if self.recorder:
            self.recorder.record(direction, data, endpoint=endpoint)

    def emit(self, state):
        state = state_for(self.profile, state)
        if state.get("modes") is False:
            self.mode_seen = False
        elif state.get("modes") is True and state.get("mode") in self.profile["capabilities"]["modes"]:
            self.mode_seen = True
        self.record("state", state)
        self.output(state)


def configure(module, context):
    """Configure only this freshly loaded module in this model's process.

    Original scripts, their tables, unnamed callers and UNKNOWN stay untouched.
    Replay calls the same function as the runtime, including capability guards.
    """
    name = context.profile["adapter"].get("builtin")
    params = context.profile["adapter"].get("parameters", {})
    if name == "sony":
        module.MODELS = {**module.MODELS, context.name: dict(params)}
    elif name == "bose":
        module.CHANNELS = tuple(params["channels"])
    elif name in ("nothing", "soundcore"):
        row = dict(params)
        if name == "soundcore":
            row["name"] = context.profile["model"]
        module.model_for = lambda _identity: dict(row)
    module.emit = context.emit
    base = module.Bridge
    command_name = "run_command" if name == "jbl" else "command"
    original = getattr(base, command_name)

    def command(self, line):
        context.record("command", line)
        if context.mode_seen and allowed(context.profile, line):
            return original(self, line)

    module.Bridge = type("DeviceBridge", (base,), {command_name: command})
    return module


class Proxy:
    def __init__(self, target, **overrides):
        self.target = target
        self.__dict__.update(overrides)

    def __getattr__(self, key):
        return getattr(self.target, key)


def observe(module, context):
    """Observe successful I/O at the transport boundary without changing queries."""
    name = context.profile["adapter"]["builtin"]
    if name in ("nothing", "bose"):
        socket_type = module.socket.socket

        class ObservedSocket(socket_type):
            def connect(self, endpoint):
                result = super().connect(endpoint)
                self.capture_endpoint = endpoint
                return result

            def sendall(self, data, *args, **kwargs):
                result = super().sendall(data, *args, **kwargs)
                context.record("tx", bytes(data), endpoint=getattr(self, "capture_endpoint", None))
                return result

            def recv(self, *args, **kwargs):
                data = super().recv(*args, **kwargs)
                context.record("rx", data, endpoint=getattr(self, "capture_endpoint", None))
                return data

        module.socket = Proxy(module.socket, socket=ObservedSocket)
    elif name == "jbl":
        base = module.Bridge

        class ObservedBridge(base):
            def tell(self, line):
                # This boundary is the client command, not a radio packet.
                context.record("tx", line)
                return super().tell(line)

            def on_client_line(self, line):
                context.record("rx", line)
                return super().on_client_line(line)

        module.Bridge = ObservedBridge
    else:
        instances = []
        base = module.Bridge

        class ObservedBridge(base):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                instances.append(self)

        def read(fd, count):
            data = os.read(fd, count)
            if any(b.fd == fd for b in instances):
                context.record("rx", data)
            return data

        def write(fd, data):
            size = os.write(fd, data)
            if any(b.fd == fd for b in instances):
                context.record("tx", bytes(data[:size]))
            return size

        module.Bridge = ObservedBridge
        module.os = Proxy(os, read=read, write=write)


def builtin_module(context, root=profiles.ROOT):
    name = context.profile["adapter"]["builtin"]
    return configure(load_module(root / (name + "-bridge")), context)


def local_module(context):
    module = load_module(profiles.inside(context.directory, "adapter.py"))
    if getattr(module, "API_VERSION", None) != 1:
        raise ValueError("adapter.py must export API_VERSION = 1")
    return module


def run(context):
    name = context.profile["adapter"].get("builtin")
    if not name:
        code = local_module(context).run(context)
        if code not in (0, 1, 3, 4):
            raise ValueError("adapter.run must return a BRIDGE.md exit code")
        return code
    module = builtin_module(context)
    if context.recorder:
        observe(module, context)
    args = [context.address]
    if name == "sony":
        uuid = next(u for u in profiles.BUILTINS["sony"]["uuids"] if u in context.uuids)
        args += [uuid, context.name]
    elif name == "nothing":
        args += [context.name]
    elif name == "jbl":
        args = [context.ble_address, context.model_id]
    sys.argv = [name + "-bridge"] + args
    if hasattr(module, "arm_parent_death_signal"):
        module.arm_parent_death_signal()
    module.main()
    return 0
