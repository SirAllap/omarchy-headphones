"""Author tools create inert drafts; evidence and protocol bytes come from owners."""
import argparse
import json
from pathlib import Path

from omaphones.registry import ROOT, ID, UUID, contained, read_json


def installed_root():
    import os
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return (config / "omarchy/plugins/io.github.ncr.omaphones").resolve()


def require_isolated(root):
    if root.resolve() == installed_root():
        raise ValueError("work in an isolated clone; writing here reloads the active plugin")


def save(path, value):
    with path.open("x") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")


def new_adapter(argv=None, root=ROOT):
    parser = argparse.ArgumentParser(description="Create a draft adapter package")
    parser.add_argument("id")
    parser.add_argument("--uuid", required=True, help="observed identifying service UUID")
    parser.add_argument("--transport", choices=("bluez-profile", "rfcomm", "ble-gatt"), default="bluez-profile")
    parser.add_argument("--channel", type=int, action="append")
    parser.add_argument("--write-handle")
    parser.add_argument("--notify-handle")
    parser.add_argument("--priority", type=int, required=True)
    args = parser.parse_args(argv)
    if not ID.fullmatch(args.id) or not UUID.fullmatch(args.uuid):
        parser.error("use a lowercase adapter id and observed UUID")
    transport = {"kind": args.transport}
    if args.transport == "bluez-profile":
        transport["uuidPreference"] = [args.uuid]
    elif args.transport == "rfcomm":
        if not args.channel or any(not 1 <= c <= 30 for c in args.channel):
            parser.error("RFCOMM requires observed --channel values (1-30)")
        transport["channels"] = args.channel
    else:
        import re
        if any(not re.fullmatch(r"0x[0-9a-f]{4}", value or "") for value in (args.write_handle, args.notify_handle)):
            parser.error("GATT requires observed --write-handle and --notify-handle")
        transport.update(writeHandle=args.write_handle, notifyHandle=args.notify_handle, addressField="bleAddress")
    require_isolated(root)
    package = root / "adapters" / args.id
    package.mkdir()  # Refuse to overwrite an existing package.
    for name in ("models", "pins", "captures", "tests"):
        (package / name).mkdir()
    save(package / "adapter.json", {
        "apiVersion": 1, "id": args.id, "status": "draft", "priority": args.priority,
        "match": {"uuids": [args.uuid]}, "entry": "protocol.py", "transport": transport,
    })
    (package / "protocol.py").write_text('''"""Fill only from this device's observed protocol; see docs/ADAPTER-API.md."""
from omaphones.api import Protocol


class Adapter(Protocol):
    def connected(self):
        pass

    def received(self, data):
        pass

    def command(self, control, value):
        pass
''')
    (package / "tests" / "protocol_test.py").write_text('''"""Add captured exchanges and synthetic fault cases before activating."""
import unittest


class EvidenceRequired(unittest.TestCase):
    def test_owner_evidence_required(self):
        self.fail("Replace this draft with captured protocol and failure-path tests")
''')
    print(package)


def new_model(argv=None, root=ROOT):
    parser = argparse.ArgumentParser(description="Create a draft model without changing another model")
    parser.add_argument("adapter")
    parser.add_argument("id")
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--name")
    identity.add_argument("--model-id")
    identity.add_argument("--uuid-suffix")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--parameters", default="{}", help="JSON protocol variant parameters")
    args = parser.parse_args(argv)
    if not ID.fullmatch(args.adapter) or not ID.fullmatch(args.id):
        parser.error("invalid adapter or model id")
    require_isolated(root)
    package = root / "adapters" / args.adapter
    row = read_json(package / "adapter.json")
    if not row.get("entry"):
        parser.error("this adapter still uses its legacy model table; migrate it first")
    parameters = json.loads(args.parameters)
    if not isinstance(parameters, dict):
        parser.error("parameters must be an object")
    match = {"name": args.name} if args.name else {"modelId": args.model_id} if args.model_id else {"uuidSuffix": args.uuid_suffix}
    (package / "models").mkdir(exist_ok=True)
    path = package / "models" / (args.id + ".json")
    save(path, {"id": args.id, "status": "draft", "match": match, "parameters": parameters,
                "owners": [args.owner], "pins": [], "captures": []})
    print(path)
