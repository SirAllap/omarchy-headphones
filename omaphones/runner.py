"""One device process. Adapters receive no OS, pipe or transport objects."""
import argparse
import json
import os
import re
import signal
from pathlib import Path

from omaphones import devices
from omaphones.recording import Recorder
from omaphones.registry import ROOT, ID

from omaphones.api import Event
from omaphones import cache
from omaphones.platform import arm_parent_death_signal, emit, GLibClock
from omaphones.registry import get_adapter, load_protocol, transport_for
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
    parser.add_argument("adapter", nargs="?")
    parser.add_argument("--device")
    parser.add_argument("--allow-draft", action="store_true")
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--owner", help="owner of an explicit migration test recording")
    parser.add_argument("--context", required=True, help="JSON device identity, never shell text")
    args = parser.parse_args(argv)
    try:
        profile = None
        if args.device:
            if args.adapter or not ID.fullmatch(args.device):
                raise ValueError("choose one valid device package")
            directory = ROOT / "devices" / args.device
            profile = devices.load(directory)
            devices.no_legacy_claim(profile)
            if profile["status"] != "active" and not args.allow_draft:
                raise ValueError("draft device is not active")
            definition = devices.resolve(profile, directory)
            row = {"entry": str(definition["entry"]), "transport": definition["transport"]}
        else:
            row = get_adapter(args.adapter)
            if row is None or not row.get("entry"):
                raise ValueError("adapter has no native API entry")
        context = json.loads(args.context)
        if not isinstance(context, dict):
            raise ValueError("context must be an object")
        field = "bleAddress" if row["transport"]["kind"] == "ble-gatt" else "address"
        if not isinstance(context.get(field), str) or not ADDRESS.fullmatch(context[field]):
            raise ValueError("invalid Bluetooth address")
        for key in ("name", "modelId", "uuid"):
            if key in context and not isinstance(context[key], str):
                raise ValueError("invalid identity field: " + key)
        if "uuids" in context and (not isinstance(context["uuids"], list) or any(not isinstance(u, str) for u in context["uuids"])):
            raise ValueError("invalid UUID list")
        transport_config = row["transport"] if profile else transport_for(row, context)
        if transport_config["kind"] == "bluez-profile" and context.get("uuids"):
            advertised = [u.lower() for u in context["uuids"]]
            uuid = next((u for u in transport_config["uuidPreference"] if u in advertised), None)
            if uuid is None:
                raise ValueError("device does not advertise the model's transport UUID")
            context["uuid"] = uuid
        if transport_config["kind"] == "bluez-profile" and context.get("uuid") and context["uuid"] not in transport_config["uuidPreference"]:
            raise ValueError("UUID is outside this adapter's profile list")
        protocol = devices.open_protocol(profile, directory, context) if profile else load_protocol(row, context)
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
    transport = TRANSPORTS[transport_config["kind"]](transport_config, context, GLib)
    recorder = None
    if args.capture:
        try:
            if not profile and (not args.owner or not re.fullmatch(r'[A-Za-z0-9-]+', args.owner) or not context.get('name')):
                raise ValueError('migration recording needs --owner and an exact device name')
            from omaphones.owner_recording import revision
            recorder = Recorder(args.capture, {"apiVersion": 1, "device": profile["id"] if profile else args.adapter,
                "owner": profile["owner"] if profile else args.owner, "context": context,
                "implementation": devices.implementation_hash(profile, directory) if profile else revision(devices.ROOT)['codeSha256'],
                "boundary": "gatt-notification/client-command-payload" if transport_config["kind"] == "ble-gatt" else "stream"})
            transport.record = recorder.record
        except (ValueError, OSError) as error:
            emit({"modes": False, "error": str(error)})
            return 4
    remembered = False
    def output(state, observed=None):
        nonlocal remembered
        if not remembered and "noise.mode" in state.values and row.get("supportCache"):
            cache.remember(context.get("modelId", ""), True)
            remembered = True
        emit({**state.legacy(), **state.snapshot(), **({"observed": observed} if observed is not None else {})})
    def observe(state, fields, changed):
        if fields or changed:
            output(state, fields)
    def ended(code, message):
        if code == 3 and not remembered and row.get("supportCache"):
            cache.remember(context.get("modelId", ""), False)
        try:
            if message:
                emit({"modes": False, "error": message})
        finally:
            loop.quit()
    limits = dict(profile["capabilities"]) if profile else None
    if profile and profile.get("batterySource") != "bridge":
        limits.pop("battery", None)
    session = Session(protocol, transport, GLibClock(GLib), output, ended, limits=limits, recorder=recorder, observer=observe if recorder else None)
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
        if recorder:
            recorder.close()
    return session.exit_code or 0
