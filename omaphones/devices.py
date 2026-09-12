"""One model, one package. Discovery never imports contributor code."""
import ast
import hashlib
import json
import re

from omaphones.registry import ROOT, ID, UUID, contained, read_json, get_adapter, validate_parameters, validate_transport, instantiate
from omaphones.state import validate_capability

ADDRESS = re.compile(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\Z")


def identity(text):
    names = re.findall(r"^\s*Name:\s*(.+)$", text, re.M)
    addresses = re.findall(r"^Device ([0-9A-Fa-f:]{17})(?:\s|$)", text, re.M)
    uuids = re.findall(r"UUID:.*\(([0-9A-Fa-f-]{36})\)", text)
    if not names or not addresses or not ADDRESS.fullmatch(addresses[0]):
        raise ValueError("identity needs complete bluetoothctl info including Device and Name")
    return {"name": names[0].strip(), "address": addresses[0].upper(), "uuids": sorted({u.lower() for u in uuids})}


def matches(profile, context):
    rule = profile["match"]
    return (context.get("name", "").strip().lower() in [n.lower() for n in rule["names"]]
            and set(rule["uuids"]).issubset(u.lower() for u in context.get("uuids", []))
            and (not rule.get("modelId") or rule["modelId"] == context.get("modelId", "").lower()))


def validate(profile):
    if set(profile) - {"apiVersion", "id", "model", "owner", "status", "match", "adapter", "capabilities", "batterySource"}:
        raise ValueError("unknown device field (no legacy evidence exemptions)")
    if type(profile.get("apiVersion")) is not int or profile["apiVersion"] != 1:
        raise ValueError("device apiVersion must be 1")
    if not isinstance(profile.get("id"), str) or not ID.fullmatch(profile["id"]):
        raise ValueError("invalid device id")
    if profile.get("status") not in ("draft", "active"):
        raise ValueError("device status must be draft or active")
    if not isinstance(profile.get("model"), str) or not profile["model"].strip():
        raise ValueError("model name is required")
    if not isinstance(profile.get("owner"), str) or not re.fullmatch(r"[A-Za-z0-9-]+", profile["owner"]):
        raise ValueError("owner must be a GitHub login without @")
    rule = profile.get("match", {})
    if not isinstance(rule, dict) or set(rule) - {"names", "uuids", "modelId"}:
        raise ValueError("invalid device match")
    names, uuids = rule.get("names"), rule.get("uuids")
    if not isinstance(names, list) or len(names) != 1 or any(not isinstance(n, str) or not n or n.strip() != n for n in names):
        raise ValueError("one exact reported name is required")
    if len({n.lower() for n in names}) != len(names):
        raise ValueError("duplicate reported names")
    if not isinstance(uuids, list) or not uuids or any(not isinstance(u, str) or not UUID.fullmatch(u) for u in uuids) or len(set(uuids)) != len(uuids):
        raise ValueError("observed lowercase UUIDs are required")
    if "modelId" in rule and (not isinstance(rule["modelId"], str) or not re.fullmatch(r"[0-9a-f]{6}", rule["modelId"])):
        raise ValueError("modelId must have six lowercase hex digits")
    adapter = profile.get("adapter")
    if not isinstance(adapter, dict) or set(adapter) - {"id", "module", "parameters", "transport"}:
        raise ValueError("invalid adapter definition")
    if ("id" in adapter) == ("module" in adapter):
        raise ValueError("choose a shared adapter id or local protocol.py")
    if "module" in adapter and adapter["module"] != "protocol.py":
        raise ValueError("local adapter must be protocol.py")
    if "id" in adapter and (not isinstance(adapter["id"], str) or not ID.fullmatch(adapter["id"])):
        raise ValueError("invalid shared adapter id")
    if not isinstance(adapter.get("parameters"), dict):
        raise ValueError("explicit adapter parameters are required")
    caps = profile.get("capabilities")
    if not isinstance(caps, dict) or "noise.mode" not in caps:
        raise ValueError("declare observed noise.mode capabilities")
    for key, spec in caps.items():
        validate_capability(key, spec)
    if "battery" in caps:
        parts = caps["battery"].get("parts")
        if not isinstance(parts, list) or not parts or set(parts) - {"left", "right", "case", "headset"} or len(set(parts)) != len(parts):
            raise ValueError("battery capability needs observed parts")
        if "headset" in parts and len(parts) != 1:
            raise ValueError("headset and earbud battery shapes cannot coexist")
        if profile.get("batterySource") not in ("bridge", "fast-pair", "bluez"):
            raise ValueError("declare batterySource")
    elif "batterySource" in profile:
        raise ValueError("batterySource needs a battery capability")
    return profile


def load(directory):
    profile = validate(read_json(directory / "device.json"))
    if profile["id"] != directory.name:
        raise ValueError("device id differs from directory")
    return profile


def protected_names(root):
    names = set()
    for path in (root / "tests/pins").glob("*/*.json"):
        pin = read_json(path)
        model = pin["model"].lower()
        names.update((model, model.removeprefix(path.parent.name + " ")))
        if pin.get("session", {}).get("name"):
            names.add(pin["session"]["name"].lower())
    for path in root.glob("*-bridge"):
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "MODELS" for t in node.targets):
                try:
                    rows = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    continue
                names.update(k.lower() for k in rows if isinstance(k, str))
                names.update(v["name"].lower() for v in rows.values() if isinstance(v, dict) and v.get("name"))
    return names


def no_legacy_claim(profile, root=ROOT):
    names = {n.lower() for n in profile["match"]["names"]}
    names.update(n.removeprefix("nothing ") for n in list(names))
    collisions = names & protected_names(root)
    if collisions:
        raise ValueError("already supported owner model: " + ", ".join(sorted(collisions)))


def packages(root=ROOT):
    rows = []
    for path in sorted((root / "devices").glob("*/device.json")):
        if read_json(path).get("status") == "draft":
            continue
        profile = load(path.parent)
        no_legacy_claim(profile, root)
        for _, peer in rows:
            overlap = {n.lower() for n in profile["match"]["names"]} & {n.lower() for n in peer["match"]["names"]}
            a, b = profile["match"].get("modelId"), peer["match"].get("modelId")
            if overlap and (not a or not b or a == b):
                raise ValueError("overlapping device identities: " + peer["id"] + " / " + profile["id"])
        rows.append((path.parent, profile))
    return rows


def resolve(profile, directory, root=ROOT):
    validate(profile)
    definition = profile["adapter"]
    params = definition["parameters"]
    if definition.get("id"):
        row = get_adapter(definition["id"], root)
        if not row or not row.get("entry"):
            raise ValueError("shared adapter has no native protocol entry")
        validate_parameters(row, params)
        overrides = definition.get("transport", {})
        if not isinstance(overrides, dict) or set(overrides) - set(row.get("modelTransportFields", [])):
            raise ValueError("undeclared model transport override")
        kind = row['transport']['kind']
        required = {'rfcomm': 'channels', 'bluez-profile': 'uuidPreference'}.get(kind)
        if required and required not in overrides:
            raise ValueError('record this model transport explicitly: ' + required)
        transport = {**row["transport"], **overrides}
        entry = contained(root / "adapters" / row["id"], row["entry"])
    else:
        transport = definition.get("transport", {})
        entry = contained(directory, definition["module"])
    validate_transport(transport)
    if transport["kind"] == "ble-gatt" and not profile["match"].get("modelId"):
        raise ValueError("BLE devices need observed Fast Pair modelId")
    if transport["kind"] == "bluez-profile" and not set(transport["uuidPreference"]) & set(profile["match"]["uuids"]):
        raise ValueError("transport UUID must be in the recorded device identity")
    if not entry.is_file():
        raise ValueError("missing protocol.py")
    return {"entry": entry, "transport": transport, "parameters": params}


def open_protocol(profile, directory, context, root=ROOT):
    if not matches(profile, context):
        raise ValueError("runtime/capture identity does not select this exact model")
    definition = resolve(profile, directory, root)
    return instantiate(definition["entry"], definition["parameters"])


def implementation_hash(profile, directory, root=ROOT):
    definition = resolve(profile, directory, root)
    digest = hashlib.sha256()
    config = {k: profile[k] for k in ("apiVersion", "id", "match", "adapter", "capabilities")}
    config["batterySource"] = profile.get("batterySource")
    digest.update(json.dumps(config, sort_keys=True).encode())
    paths = [root / "omaphones" / name for name in ("api.py", "session.py", "state.py", "runner.py", "registry.py", "devices.py", "transports.py", "platform.py", "cache.py", "recording.py", "live.py")]
    paths += [root / "omaphones-device", root / "tools/add-device", definition["entry"]]
    paths += [root / n for n in ("DeviceFollower.qml", "Service.qml", "Panel.qml", "gfps-reader")]
    digest.update(json.dumps(definition["transport"], sort_keys=True).encode())
    source = (root / "Model.js").read_text()
    source = re.sub(r"// BEGIN GENERATED ADAPTER REGISTRY.*?// END GENERATED ADAPTER REGISTRY", "", source, flags=re.S)
    digest.update(source.encode())
    for path in sorted(set(paths)):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()
