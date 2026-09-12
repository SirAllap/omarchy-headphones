"""Profiles are data; discovery never imports contributor code or opens a link."""
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
ADDRESS = re.compile(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\Z")
MODES = ("off", "anc", "ambient", "talkthru")
LEVELS = ("low", "mid", "high", "adaptive")
BUILTINS = {
    "sony": {"uuids": ["956c7b26-d49a-4ba8-b03f-b17d393cb6e2",
                       "96cc203e-5068-46ad-b32d-e316f5e069ba"]},
    "samsung": {"uuids": ["2e73a4ad-332d-41fc-90e2-16bef06523f2"]},
    "nothing": {"uuids": ["aeac4a03-dff5-498f-843a-34487cf133eb"]},
    "xiaomi": {"uuids": ["00001100-d102-11e1-9b23-00025b00a5a5"]},
    "soundcore": {"prefix": "0cf12d31-fac3-4553-bd80-d6832e7"},
    "oppo": {"uuids": ["0000079a-d102-11e1-9b23-00025b00a5a5"]},
    "bose": {"uuids": ["00000000-deca-fade-deca-deafdecacaff", "9b26d8c0-a8ed-440b-95b0-c4714a518bcc"]},
    "jbl": {"fastPair": True},
}


def read_json(path):
    return json.loads(Path(path).read_text())


def inside(directory, relative):
    """Artifact paths cannot escape their device package, including symlinks."""
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("expected a relative artifact path")
    path = (directory / relative).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError("artifact escapes device package: " + relative)
    return path


def identity(text):
    names = re.findall(r"^\s*Name:\s*(.+)$", text, re.M)
    addresses = re.findall(r"^Device ([0-9A-Fa-f:]{17})(?:\s|$)", text, re.M)
    uuids = re.findall(r"UUID:.*\(([0-9A-Fa-f-]{36})\)", text)
    if not names or not addresses or not ADDRESS.fullmatch(addresses[0]):
        raise ValueError("identity needs complete bluetoothctl info output, including Device and Name")
    return {"name": names[0].strip(), "address": addresses[0].upper(),
            "uuids": sorted(set(u.lower() for u in uuids))}


def candidates(uuids):
    return [name for name, row in BUILTINS.items()
            if any(u in row.get("uuids", []) or
                   (row.get("prefix") and u.startswith(row["prefix"])) for u in uuids)]


def validate(profile):
    """Strict shape checks run before runtime imports or Bluetooth activity."""
    def require(condition, message):
        if not condition:
            raise ValueError(message)

    require(isinstance(profile, dict), "profile must be an object")
    require(type(profile.get("apiVersion")) is int and profile["apiVersion"] == 1, "apiVersion must be 1")
    require(isinstance(profile.get("id"), str) and SLUG.fullmatch(profile["id"]), "invalid device id")
    for key in ("model", "owner"):
        require(isinstance(profile.get(key), str) and profile[key].strip(), key + " is required")
    require(re.fullmatch(r"[A-Za-z0-9-]+", profile["owner"]), "owner must be a GitHub login without @")
    match = profile.get("match", {})
    require(isinstance(match, dict), "match must be an object")
    names, uuids = match.get("names"), match.get("uuids")
    require(isinstance(names, list) and names and all(isinstance(n, str) and n.strip() == n and n for n in names),
            "match.names needs exact reported names")
    require(len(set(n.casefold() for n in names)) == len(names), "duplicate reported names")
    require(isinstance(uuids, list) and uuids and all(isinstance(u, str) and UUID.fullmatch(u) for u in uuids),
            "match.uuids needs lowercase UUIDs (all must be present)")
    require(len(set(uuids)) == len(uuids), "duplicate UUIDs")
    fp = match.get("modelId")
    require(fp is None or isinstance(fp, str) and re.fullmatch(r"[0-9a-f]{6}", fp),
            "match.modelId must be six lowercase hex digits")
    adapter = profile.get("adapter", {})
    require(isinstance(adapter, dict), "adapter must be an object")
    name = adapter.get("builtin")
    require((isinstance(name, str) and name in BUILTINS) != (adapter.get("module") == "adapter.py"),
            "choose one builtin or local adapter.py")
    require(adapter.get("transport") in ("classic", "ble"), "adapter.transport must be classic or ble")
    if name:
        require(isinstance(name, str) and name in BUILTINS, "unknown builtin")
        require(adapter["transport"] == ("ble" if name == "jbl" else "classic"), "wrong builtin transport")
        require(name == "jbl" or name in candidates(uuids), "builtin does not match the recorded UUIDs")
    if adapter["transport"] == "ble":
        require(fp is not None, "BLE profiles need an exact Fast Pair modelId")
    params = adapter.get("parameters", {})
    require(isinstance(params, dict), "adapter.parameters must be an object")
    if name in ("nothing", "bose"):
        require(set(params) == {"channels"} and isinstance(params["channels"], list)
                and params["channels"] and all(type(c) is int and 1 <= c <= 30 for c in params["channels"]),
                "this adapter needs explicit observed channels, each 1..30")
    elif name == "sony":
        require(set(params) == {"wear"} and type(params["wear"]) is bool,
                "Sony needs explicit wear: true or false")
    elif name == "soundcore":
        require(set(params) == {"offset", "query"} and type(params["offset"]) is int
                and 0 <= params["offset"] <= 4096 and type(params["query"]) is bool,
                "Soundcore needs explicit offset and query")
    elif name:
        require(not params, "this builtin has no model parameters")
    caps = profile.get("capabilities", {})
    require(isinstance(caps, dict), "capabilities must be an object")
    require(not set(caps) - {"modes", "ambient", "ancLevels", "latency", "worn", "battery"},
            "unknown capability; extend the contract separately")
    modes = caps.get("modes", [])
    require(isinstance(modes, list) and modes and all(m in MODES for m in modes)
            and len(set(modes)) == len(modes), "declare the confirmed listening modes")
    levels = caps.get("ancLevels", [])
    require(isinstance(levels, list) and all(v in LEVELS for v in levels)
            and len(set(levels)) == len(levels) and (not levels or "anc" in modes), "invalid ancLevels")
    for key in ("latency", "worn"):
        require(type(caps.get(key, False)) is bool, key + " must be boolean")
    ambient = caps.get("ambient")
    if ambient is not None:
        require(isinstance(ambient, dict) and "ambient" in modes, "ambient needs ambient mode")
        require(type(ambient.get("min")) is int and type(ambient.get("max")) is int
                and 0 <= ambient["min"] < ambient["max"] <= 100, "invalid ambient range")
        require(ambient.get("voiceCommand") in ("voice", "wind"), "ambient.voiceCommand must be voice or wind")
        require(isinstance(ambient.get("voice"), str) and ambient["voice"], "ambient.voice label is required")
    battery = caps.get("battery")
    if battery is not None:
        require(isinstance(battery, dict) and battery.get("source") in ("bridge", "fast-pair", "bluez"),
                "battery needs source: bridge, fast-pair or bluez")
        parts = battery.get("parts")
        require(isinstance(parts, list) and parts and all(p in ("headset", "left", "right", "case") for p in parts)
                and len(set(parts)) == len(parts), "invalid battery parts")
        require(not ("headset" in parts and len(parts) != 1), "headset battery cannot coexist with earbuds")
    return profile


def load(directory):
    profile = validate(read_json(directory / "device.json"))
    if profile["id"] != directory.name:
        raise ValueError("directory must have the same name as device.id")
    return profile


def packages(root=ROOT):
    paths = sorted((root / "devices").glob("*/device.json"))
    for path in paths:
        if not path.resolve().is_relative_to((root / "devices").resolve()):
            raise ValueError("device package escapes devices/: " + str(path))
    return paths


def match_profile(profile, name, uuids, model_id=""):
    rule = profile["match"]
    return (name.strip().lower() in [n.lower() for n in rule["names"]]
            and set(rule["uuids"]).issubset(u.lower() for u in uuids)
            and (not rule.get("modelId") or rule["modelId"] == model_id.lower()))


def registry(root=ROOT):
    rows = []
    for path in packages(root):
        profile = load(path.parent)
        rows.append({key: profile[key] for key in ("id", "model", "match", "adapter", "capabilities")})
    # UUID subsets can coexist on one device, so overlapping names with compatible
    # model IDs are ambiguous even if the two UUID requirements differ.
    for i, row in enumerate(rows):
        for other in rows[:i]:
            names = {n.casefold() for n in row["match"]["names"]}
            overlap = names.intersection(n.casefold() for n in other["match"]["names"])
            left, right = row["match"].get("modelId"), other["match"].get("modelId")
            if overlap and (not left or not right or left == right):
                raise ValueError("overlapping device profiles: " + other["id"] + " / " + row["id"])
    return rows


def implementation_hash(profile, directory, root=ROOT):
    """Bind a live report to executable content; no circular dependency on reports."""
    digest = hashlib.sha256()
    runtime = {k: profile[k] for k in ("apiVersion", "id", "match", "adapter", "capabilities")}
    digest.update(json.dumps(runtime, sort_keys=True).encode())
    files = list((root / "adapter_api").glob("*.py"))
    files += [root / "device-adapter", root / "DeviceFollower.qml", root / "Service.qml", root / "Panel.qml", root / "gfps-reader"]
    name = profile["adapter"].get("builtin")
    files += [root / (name + "-bridge")] if name else list(directory.rglob("*.py"))
    # Other profiles and their generated registry do not invalidate this model's report.
    source = (root / "Model.js").read_text()
    source = re.sub(r"// BEGIN DEVICE REGISTRY.*?// END DEVICE REGISTRY", "", source, flags=re.S)
    digest.update(source.encode())
    for path in sorted(files):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def cases(profile):
    caps = profile["capabilities"]
    out = {"initial", "external-change", "repeated", "unsupported-command", "reconnect", "restoration"}
    out.update("mode:" + mode for mode in caps["modes"])
    out.update("ancLevel:" + value for value in caps.get("ancLevels", []))
    if caps.get("ambient"):
        ambient = caps["ambient"]
        out.update("ambient:" + str(v) for v in (ambient["min"], ambient["max"]))
        out.update(("voice:on", "voice:off"))
    if caps.get("latency"):
        out.update(("latency:on", "latency:off"))
    if caps.get("worn"):
        out.update(("worn:on", "worn:off"))
    out.update("battery:" + part for part in caps.get("battery", {}).get("parts", []))
    return sorted(out)
