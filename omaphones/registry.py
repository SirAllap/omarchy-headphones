"""Declarative adapter discovery, validation and deterministic routing."""
import importlib.util
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent
ID = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
KINDS = {"bluez-profile", "rfcomm", "ble-gatt"}


def contained(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("path leaves its package: " + relative)
    return path


def read_json(path):
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(str(path) + ": expected a JSON object")
    return value


def descriptors(root=ROOT, drafts=False):
    rows = []
    for path in sorted((root / "adapters").glob("*/adapter.json")):
        row = read_json(path)
        if row.get("status") == "draft" and not drafts:
            continue
        validate(row, path.parent, root, drafts=drafts)
        rows.append(row)
    priorities = [row["priority"] for row in rows]
    if len(priorities) != len(set(priorities)):
        raise ValueError("adapter priorities must be unique")
    for index, row in enumerate(rows):
        match = row["match"]
        for peer in rows[index + 1:]:
            other = peer["match"]
            prefix, other_prefix = match.get("uuidPrefix"), other.get("uuidPrefix")
            overlap = set(match.get("uuids", [])) & set(other.get("uuids", []))
            overlap = overlap or (match.get("ble") and other.get("ble"))
            overlap = overlap or (prefix and any(u.startswith(prefix) for u in other.get("uuids", [])))
            overlap = overlap or (other_prefix and any(u.startswith(other_prefix) for u in match.get("uuids", [])))
            overlap = overlap or (prefix and other_prefix and (prefix.startswith(other_prefix) or other_prefix.startswith(prefix)))
            if overlap:
                raise ValueError("overlapping adapter claims: " + row["id"] + " and " + peer["id"])
    fallback = [row for row in rows if row['match'].get('ble')]
    if fallback and any(row['priority'] >= fallback[0]['priority'] for row in rows if row is not fallback[0]):
        raise ValueError("BLE fallback must follow every UUID claim")
    return sorted(rows, key=lambda row: (row["priority"], row["id"]))


def validate(row, package, root=ROOT, drafts=False):
    if type(row.get("apiVersion")) is not int or row["apiVersion"] != 1:
        raise ValueError("unsupported adapter API version")
    if not ID.fullmatch(row.get("id", "")) or row["id"] != package.name:
        raise ValueError("adapter id must equal its directory name")
    if type(row.get("priority")) is not int:
        raise ValueError("priority must be an integer")
    if row.get("status") not in ("active", "draft"):
        raise ValueError("status must be active or draft")
    match = row.get("match", {})
    if not isinstance(match, dict) or not match or set(match) - {"uuids", "uuidPrefix", "ble"}:
        raise ValueError("declare UUIDs, a UUID prefix or the BLE fallback")
    if "uuids" in match and (not isinstance(match["uuids"], list) or not match["uuids"]):
        raise ValueError("UUID list must be nonempty")
    for uuid in match.get("uuids", []):
        if not isinstance(uuid, str) or not UUID.fullmatch(uuid):
            raise ValueError("invalid match UUID")
    if "uuidPrefix" in match and not re.fullmatch(r"[0-9a-f-]{8,35}", match["uuidPrefix"]):
        raise ValueError("invalid UUID prefix")
    if "ble" in match and match["ble"] is not True:
        raise ValueError("ble must be true")
    if row.get("entry"):
        if not contained(package, row["entry"]).is_file():
            raise ValueError("adapter entry does not exist")
        validate_transport(row.get("transport", {}))
    elif not row.get("legacy"):
        raise ValueError("adapter has neither an entry nor a legacy bridge")
    if row.get("legacy"):
        legacy = row["legacy"]
        if not contained(root, legacy["bridge"]).is_file():
            raise ValueError("legacy bridge does not exist")
        if set(legacy["args"]) - {"address", "uuid", "name", "bleAddress", "modelId"}:
            raise ValueError("unknown legacy argument")
    for parameters in (row.get("unknownModel", {}), row.get("namelessModel", {})):
        validate_parameters(row, parameters)
    if set(row.get("modelTransportFields", [])) - MODEL_TRANSPORT_FIELDS:
        raise ValueError("unsupported model transport field")
    seen = set()
    for path in sorted((package / "models").glob("*.json")):
        model = read_json(path)
        if model.get("status") == "draft" and not drafts:
            continue
        if model.get("status", "active") not in ("active", "draft"):
            raise ValueError("invalid model status")
        if not ID.fullmatch(model.get("id", "")) or model["id"] != path.stem:
            raise ValueError("invalid model id: " + str(path))
        identity = json.dumps(model.get("match"), sort_keys=True)
        if identity in seen:
            raise ValueError("duplicate model match: " + identity)
        seen.add(identity)
        if set(model.get("match", {})) - {"name", "modelId", "uuidSuffix"} or not model.get("match"):
            raise ValueError("model needs an exact identity")
        if any(not isinstance(value, str) or not value for value in model["match"].values()):
            raise ValueError("model identity values must be nonempty strings")
        validate_parameters(row, model.get("parameters"))
        overrides = model.get("transport", {})
        if not isinstance(overrides, dict) or set(overrides) - set(row.get("modelTransportFields", [])):
            raise ValueError("model overrides an undeclared transport field")
        if overrides:
            validate_transport({**row["transport"], **overrides})
        for field in ("owners", "captures", "pins"):
            if not isinstance(model.get(field, []), list) or any(not isinstance(value, str) or not value for value in model.get(field, [])):
                raise ValueError("model " + field + " must be a list of nonempty strings")
        for ref in model.get("captures", []) + model.get("pins", []):
            if not contained(root, ref).is_file():
                raise ValueError("missing evidence: " + ref)
        if model.get("evidence") != "existing" and row["status"] == "active" and model.get("status", "active") == "active":
            if not model.get("owners") or not model.get("pins") or not model.get("captures"):
                raise ValueError("new active model needs owner, pin and capture")


MODEL_TRANSPORT_FIELDS = {"channels", "uuidPreference", "writeHandle", "notifyHandle"}


def validate_transport(transport):
    for field in ("replyTimeout", "connectTimeout", "connectDelay", "connectRetry", "connectAttempts", "discoveryTimeout", "registerTimeout"):
        if field in transport and (type(transport[field]) is not int or transport[field] <= 0):
            raise ValueError("transport timing/count must be a positive integer: " + field)
    if transport.get("kind") not in KINDS:
        raise ValueError("unknown transport")
    if transport["kind"] == "bluez-profile":
        uuids = transport.get("uuidPreference", [])
        if not uuids or any(not UUID.fullmatch(u) for u in uuids):
            raise ValueError("profile transport needs UUID preference")
    if transport["kind"] == "ble-gatt":
        for field in ("writeHandle", "notifyHandle"):
            if not re.fullmatch(r"0x[0-9a-f]{4}", transport.get(field, "")):
                raise ValueError("invalid GATT handle")
    if transport["kind"] == "rfcomm":
        if not transport.get("channels") or any(type(c) is not int or not 1 <= c <= 30 for c in transport["channels"]):
            raise ValueError("RFCOMM channels must be integers 1-30")


def validate_parameters(row, parameters):
    if not isinstance(parameters, dict):
        raise ValueError("model parameters must be an object")
    schema = row.get("parameterSchema", {})
    if set(parameters) - set(schema):
        raise ValueError("undeclared model parameter")
    types = {"boolean": bool, "integer": int, "string": str, "array": list}
    for key, spec in schema.items():
        if spec.get("type") not in types:
            raise ValueError("unsupported parameter schema type")
        if spec.get("required") and key not in parameters:
            raise ValueError("missing model parameter: " + key)
        if key in parameters and type(parameters[key]) is not types[spec["type"]]:
            raise ValueError("invalid model parameter: " + key)
        if key in parameters and "values" in spec and parameters[key] not in spec["values"]:
            raise ValueError("unsupported model parameter value: " + key)


def get_adapter(adapter_id, root=ROOT):
    return next((r for r in descriptors(root) if r["id"] == adapter_id), None)


def select(uuids, ble_address="", root=ROOT):
    ids = [u.strip().lower() for u in uuids]
    for row in descriptors(root):
        match = row["match"]
        if match.get("ble") and ble_address:
            return row["id"]
        if any(u in match.get("uuids", []) or (match.get("uuidPrefix") and u.startswith(match["uuidPrefix"])) for u in ids):
            return row["id"]
    return ""


def selected_model(row, context, root=ROOT):
    package = root / "adapters" / row["id"]
    matches = []
    for path in sorted((package / "models").glob("*.json")):
        model = read_json(path)
        if model.get("status") == "draft":
            continue
        def agrees(key, value):
            if key == "uuidSuffix":
                return any(u.lower().endswith(value.lower()) for u in context.get("uuids", []))
            return context.get(key, "") == value
        if all(agrees(k, v) for k, v in model["match"].items()):
            matches.append(model)
    if len(matches) > 1:
        raise ValueError("ambiguous model identity")
    return matches[0] if matches else None


def model_parameters(row, context, root=ROOT):
    model = selected_model(row, context, root)
    if model is not None:
        return model["parameters"]
    return row.get("namelessModel", row.get("unknownModel", {})) if not context.get("name") else row.get("unknownModel", {})


def transport_for(row, context, root=ROOT):
    model = selected_model(row, context, root)
    return {**row["transport"], **(model.get("transport", {}) if model else {})}


def load_protocol(row, context, root=ROOT):
    path = contained(root / "adapters" / row["id"], row["entry"])
    spec = importlib.util.spec_from_file_location("omaphones_adapter_" + row["id"].replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Adapter(model_parameters(row, context, root))


def shell_rows(root=ROOT):
    rows = []
    for row in descriptors(root):
        legacy = row.get("legacy", {})
        item = {"name": row["id"], "bridge": legacy.get("bridge", "omaphones-device"), **row["match"],
                "args": legacy.get("args", []), "runtime": bool(row.get("entry")), "controls": legacy.get("controls", {})}
        item["needsBleAddress"] = row.get("transport", {}).get("kind") == "ble-gatt" or bool(row["match"].get("ble"))
        item["supportCache"] = row.get("supportCache", "")
        if legacy.get("ambient"):
            item["ambient"] = legacy["ambient"]
        item["uuidPreference"] = row.get("transport", {}).get("uuidPreference", row["match"].get("uuids", []))
        rows.append(item)
    return rows
