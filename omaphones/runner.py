"""One device process. Adapters receive no OS, pipe or transport objects."""
import argparse
import json
import os
import re
import signal

from omaphones.api import Event
from omaphones import cache
from omaphones.platform import arm_parent_death_signal, emit, GLibClock
from omaphones.registry import get_adapter, load_protocol
from omaphones.session import Session
from omaphones.state import legacy_command

ADDRESS = re.compile(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\Z")


def parse_command(line):
    if line.lstrip().startswith("{"):
        try:
            request = json.loads(line)
        except ValueError:
            return None
        if not isinstance(request, dict) or type(request.get("apiVersion")) is not int or request.get("apiVersion") != 1 or not isinstance(request.get("control"), str) or "value" not in request:
            return None
        return request["control"], request["value"]
    return legacy_command(line)


def main(argv=None):
    arm_parent_death_signal()
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            emit({"modes": False, "error": message})
            raise SystemExit(4)
    parser = Parser(description="Omaphones adapter host, API v1")
    parser.add_argument("adapter")
    parser.add_argument("--context", required=True, help="JSON device identity, never shell text")
    args = parser.parse_args(argv)
    try:
        row = get_adapter(args.adapter)
        if row is None or not row.get("entry"):
            raise ValueError("adapter has no native API entry")
        context = json.loads(args.context)
        if not isinstance(context, dict):
            raise ValueError("context must be an object")
        field = row["transport"].get("addressField", "address")
        if not isinstance(context.get(field), str) or not ADDRESS.fullmatch(context[field]):
            raise ValueError("invalid Bluetooth address")
        for key in ("name", "modelId", "uuid"):
            if key in context and not isinstance(context[key], str):
                raise ValueError("invalid identity field: " + key)
        if "uuids" in context and (not isinstance(context["uuids"], list) or any(not isinstance(u, str) for u in context["uuids"])):
            raise ValueError("invalid UUID list")
        if row["transport"]["kind"] == "bluez-profile" and context.get("uuid") and context["uuid"] not in row["transport"]["uuidPreference"]:
            raise ValueError("UUID is outside this adapter's profile list")
        protocol = load_protocol(row, context)
    except Exception as error:
        emit({"modes": False, "error": str(error)})
        return 4
    try:
        from gi.repository import GLib
        if row["transport"]["kind"] == "bluez-profile":
            import dbus.mainloop.glib
            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        from omaphones.transports import TRANSPORTS
    except ImportError as error:
        emit({"modes": False, "error": "missing python dependency: " + str(error)})
        return 4
    loop = GLib.MainLoop()
    transport = TRANSPORTS[row["transport"]["kind"]](row["transport"], context, GLib)
    remembered = False
    def output(state):
        nonlocal remembered
        if not remembered and "noise.mode" in state.values and row.get("supportCache"):
            cache.remember(context.get("modelId", ""), True)
            remembered = True
        emit({**state.legacy(), **state.snapshot()})
    def ended(code, message):
        if code == 3 and not remembered and row.get("supportCache"):
            cache.remember(context.get("modelId", ""), False)
        try:
            if message:
                emit({"modes": False, "error": message})
        finally:
            loop.quit()
    session = Session(protocol, transport, GLibClock(GLib), output, ended)
    transport.deliver = session.dispatch
    pending = ""
    def stdin_ready(fd, condition):
        nonlocal pending
        if condition & GLib.IO_IN:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                chunk = b""
            if not chunk:
                session.finish(0)
                return False
            pending += chunk.decode("utf-8", "replace")
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                command = parse_command(line)
                if command:
                    control, value = command
                    session.dispatch(Event("command", value, control))
            if len(pending) > 65536:
                session.finish(4, "command line too long")
                return False
        if condition & (GLib.IO_HUP | GLib.IO_ERR):
            session.finish(0)
            return False
        return session.exit_code is None
    def stop(*_args):
        session.finish(0)
        return False
    try:
        from gi.repository import GLibUnix
        add_signal = GLibUnix.signal_add
    except ImportError:
        add_signal = GLib.unix_signal_add
    sources = [add_signal(GLib.PRIORITY_DEFAULT, signum, stop) for signum in (signal.SIGTERM, signal.SIGINT)]
    sources.append(GLib.io_add_watch(0, GLib.PRIORITY_DEFAULT, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, stdin_ready))
    try:
        transport.start()
        if session.exit_code is None:
            loop.run()
    except FileNotFoundError as error:
        session.finish(4, str(error))
    except Exception as error:
        session.finish(1, "%s: %s" % (type(error).__name__, error))
    finally:
        session.finish(0)
    return session.exit_code or 0
