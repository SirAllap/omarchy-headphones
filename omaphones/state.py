"""Host-owned capability validation, observed state and old shell projection."""
from copy import deepcopy

MODES = ("off", "anc", "ambient", "talkthru")
ENUMS = {"noise.mode": MODES, "anc.strength": ("low", "mid", "high", "adaptive")}
BOOLEANS = {"ambient.focus_on_voice", "noise.wind_reduction", "audio.low_latency", "wear.detected"}
CONTROLS = set(ENUMS) | BOOLEANS | {"ambient.level", "battery"}


def validate_capability(key, spec):
    if key not in CONTROLS or not isinstance(spec, dict):
        raise ValueError("unknown capability: " + key)
    if 'readOnly' in spec and type(spec['readOnly']) is not bool:
        raise ValueError('readOnly must be boolean')
    expected_type = 'enum' if key in ENUMS else 'number' if key == 'ambient.level' else None
    if expected_type and 'type' in spec and spec['type'] != expected_type:
        raise ValueError('invalid capability type: ' + key)
    if key in ENUMS:
        values = spec.get("values")
        if not isinstance(values, list) or not values or any(v not in ENUMS[key] for v in values) or len(values) != len(set(values)):
            raise ValueError("invalid choices: " + key)
    elif key == "ambient.level":
        if any(type(spec.get(k)) is not int for k in ("min", "max", "step")) or spec["min"] > spec["max"] or spec["step"] < 1:
            raise ValueError("invalid numeric range")
    elif key in BOOLEANS and spec.get("type") != "boolean":
        raise ValueError("boolean capability expected")
    if key in ("wear.detected", "battery") and spec.get("readOnly") is not True:
        raise ValueError("sensor must be read only")


def valid_value(key, value, spec):
    if key in ENUMS:
        return value in spec["values"]
    if key in BOOLEANS:
        return type(value) is bool
    if key == "ambient.level":
        return type(value) is int and spec["min"] <= value <= spec["max"] and (value-spec["min"]) % spec["step"] == 0
    if key == "battery":
        if not isinstance(value, dict) or set(value) - {"left", "right", "case", "headset", "charging", "caseStale"}:
            return False
        for part, reading in value.items():
            if part == "charging":
                if not isinstance(reading, list) or any(p not in ("left", "right", "case", "headset") for p in reading):
                    return False
            elif part == "caseStale":
                if type(reading) is not bool:
                    return False
            elif type(reading) is not int or not -1 <= reading <= 100:
                return False
        return True
    return False


class State:
    def __init__(self, limits=None):
        self.values = {}
        self.capabilities = {}
        self.limits = deepcopy(limits)

    def apply(self, report):
        if self.limits is not None:
            from omaphones.api import Report
            limited = {}
            for key, spec in report.capabilities.items():
                validate_capability(key, spec)
                if key not in self.limits:
                    continue
                approved = deepcopy(self.limits[key])
                if key in ENUMS:
                    approved['values'] = [v for v in approved['values'] if v in spec['values']]
                    if not approved['values']:
                        continue
                elif key == 'ambient.level':
                    if approved['min'] < spec['min'] or approved['max'] > spec['max'] or approved['step'] % spec['step'] or (approved['min'] - spec['min']) % spec['step']:
                        raise ValueError('declared range exceeds device report')
                if spec.get('readOnly'):
                    approved['readOnly'] = True
                limited[key] = approved
            values = {k: deepcopy(v) for k, v in report.values.items() if k in self.limits and (k in limited or k in self.capabilities)}
            if 'battery' in values:
                parts = self.limits['battery'].get('parts', [])
                values['battery'] = {k: v for k, v in values['battery'].items() if k in parts or k in ('charging', 'caseStale')}
                if 'charging' in values['battery']:
                    values['battery']['charging'] = [p for p in values['battery']['charging'] if p in parts]
            report = Report(values, limited)
        caps = deepcopy(self.capabilities)
        for key, spec in report.capabilities.items():
            validate_capability(key, spec)
            caps[key] = deepcopy(spec)
        values = deepcopy(self.values)
        for key, value in report.values.items():
            if key not in caps or not valid_value(key, value, caps[key]):
                raise ValueError("invalid device report: " + key)
            values[key] = deepcopy(value)
        # A capability update may narrow an enum only with a consistent report.
        if any(not valid_value(k, v, caps[k]) for k, v in values.items()):
            raise ValueError("capability update invalidates observed state")
        changed = values != self.values or caps != self.capabilities
        self.values, self.capabilities = values, caps
        return changed

    def accepts(self, control, value):
        spec = self.capabilities.get(control)
        return bool(spec and control in self.values and not spec.get("readOnly") and valid_value(control, value, spec))

    def snapshot(self):
        return {"apiVersion": 1, "values": deepcopy(self.values), "capabilities": deepcopy(self.capabilities)}

    def legacy(self):
        """Projection for existing IPC/panel consumers during migration."""
        values, caps = self.values, self.capabilities
        out = {"modes": "noise.mode" in values}
        mapping = {"noise.mode": "mode", "ambient.level": "level", "ambient.focus_on_voice": "voice",
                   "noise.wind_reduction": "voice", "anc.strength": "ancLevel", "audio.low_latency": "latency",
                   "wear.detected": "worn", "battery": "battery"}
        for key, field in mapping.items():
            if key in values:
                out[field] = deepcopy(values[key])
        if "noise.mode" in caps and caps["noise.mode"]["values"] != list(MODES):
            out["available"] = list(caps["noise.mode"]["values"])
        if "anc.strength" in caps:
            out["ancLevels"] = list(caps["anc.strength"]["values"])
        return out


def legacy_command(line, toggle="ambient.focus_on_voice"):
    """Old public command spelling, kept in the host and never in adapters."""
    parts = line.strip().split()
    if len(parts) != 2:
        return None
    verb, value = parts
    if verb == "set" and value in MODES:
        return "noise.mode", value
    if verb == "level":
        if value in ENUMS["anc.strength"]:
            return "anc.strength", value
        try:
            return "ambient.level", int(value)
        except ValueError:
            return None
    if verb in ("voice", "wind", "latency") and value in ("on", "off"):
        return {"voice": toggle, "wind": "noise.wind_reduction", "latency": "audio.low_latency"}[verb], value == "on"
    return None
