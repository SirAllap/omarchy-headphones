"""One package, one readiness report. Missing evidence is a failed check."""
from pathlib import Path
import io
import json
import re
import subprocess
import unittest

from . import live, devices as profiles, evidence as replay
from .registry import shell_rows
from . import contracts, feedback
from .checking import check_boundary
import importlib.util

FAULTS = ("silence", "disconnect", "invalid_frame", "coalesced", "repeated", "unsupported_command")


def generate(root=profiles.ROOT, write=False):
    if write:
        from .scaffold import require_isolated
        require_isolated(root)
    rows = [profile for _, profile in profiles.packages(root)]
    block = "// BEGIN GENERATED ADAPTER REGISTRY\nvar BACKENDS = " + json.dumps(shell_rows(root), indent=2) + "\n// END GENERATED ADAPTER REGISTRY"
    path = root / "Model.js"
    source = path.read_text()
    pattern = r"// BEGIN GENERATED ADAPTER REGISTRY.*?// END GENERATED ADAPTER REGISTRY"
    if len(re.findall(pattern, source, re.S)) != 1:
        raise ValueError("Model.js needs exactly one generated registry block")
    outputs = {path: re.sub(pattern, lambda _m: block, source, flags=re.S)}
    text = "<!-- BEGIN DEVICE PACKAGES -->\n"
    if rows:
        text += "\nModels contributed through the device API:\n\n| Model | Confirmed controls | Owner |\n|:--|:--|:--|\n"
        for row in rows:
            profile = profiles.load(root / "devices" / row["id"])
            model = profile["model"].replace("|", "\\|").replace("\n", " ")
            modes = " · ".join(profile["capabilities"]["noise.mode"]["values"])
            owner = profile["owner"]
            text += f"| [{model}](devices/{row['id']}/device.json) | {modes} | [@{owner}](https://github.com/{owner}) |\n"
        text += "\n"
        for row in rows:
            text += f"[![{row['id']}](devices/{row['id']}/screenshot.png)](devices/{row['id']}/screenshot.png)\n\n"
    text += "<!-- END DEVICE PACKAGES -->"
    path = root / "README.md"
    source = path.read_text()
    pattern = r"<!-- BEGIN DEVICE PACKAGES -->.*?<!-- END DEVICE PACKAGES -->"
    if len(re.findall(pattern, source, re.S)) != 1:
        raise ValueError("README needs exactly one generated device section")
    outputs[path] = re.sub(pattern, lambda _m: text, source, flags=re.S)
    changed = []
    for path, content in outputs.items():
        if path.read_text() != content:
            changed.append(str(path.relative_to(root)))
            if write:
                path.write_text(content)
    if changed and not write:
        raise ValueError("generated files are stale: " + ", ".join(changed) + "; run tools/add-device sync")
    return changed


def valid_png(data):
    import struct
    import zlib
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        return False
    offset, kinds = 8, []
    while offset + 12 <= len(data):
        size = struct.unpack_from('>I', data, offset)[0]
        kind = data[offset + 4:offset + 8]
        end = offset + 8 + size
        if end + 4 > len(data) or zlib.crc32(data[offset + 4:end]) != struct.unpack_from('>I', data, end)[0]:
            return False
        if not kinds and (kind != b'IHDR' or size != 13 or not all(struct.unpack_from('>II', data, offset + 8))):
            return False
        kinds.append(kind)
        offset = end + 4
        if kind == b'IEND':
            return size == 0 and offset == len(data) and b'IDAT' in kinds
    return False


def fault_tests(directory):
    path = directory / "test_adapter.py"
    spec = importlib.util.spec_from_file_location("device_faults_" + directory.name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)

    def flatten(suite):
        for test in suite:
            if isinstance(test, unittest.TestSuite):
                yield from flatten(test)
            else:
                yield test

    ids = [test.id().rsplit(".", 1)[-1] for test in flatten(suite)]
    missing = ["test_" + name for name in FAULTS if "test_" + name not in ids]
    if missing:
        raise ValueError("missing model fault scenarios: " + ", ".join(missing))
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=1).run(suite)
    if not result.wasSuccessful() or result.skipped or result.expectedFailures:
        raise ValueError(output.getvalue().strip() + "; skipped or expected-failure scenarios are incomplete")
    return str(result.testsRun) + " model fault tests"


def readiness(directory, root=profiles.ROOT):
    result = {"device": directory.name, "checks": []}

    def check(label, fn):
        try:
            detail = fn()
            result["checks"].append({"check": label, "passed": True, "detail": detail or "OK"})
            return detail
        except (Exception, SystemExit) as error:
            item = {"check": label, "passed": False, "detail": str(error),
                    "line": getattr(error, "lineno", 1) or 1}
            filename = getattr(error, 'filename', None)
            if isinstance(filename, str) and Path(filename).is_relative_to(root):
                item['file'] = str(Path(filename).relative_to(root))
            result['checks'].append(item)
            return None

    profile = check("profile", lambda: profiles.load(directory))
    if profile is None:
        result["passed"] = False
        return feedback.decorate(result, directory, root)
    result["checks"][-1]["detail"] = "API v1"
    check("existing models", lambda: profiles.no_legacy_claim(profile, root))

    def identification():
        record = profiles.identity((directory / "identity.txt").read_text())
        if record["name"] not in profile["match"]["names"] or not set(profile["match"]["uuids"]).issubset(record["uuids"]):
            raise ValueError("profile name/UUIDs are absent from identity.txt")
        return "reported name and UUIDs match identity.txt"

    check("identification", identification)

    def adapter():
        definition = profiles.resolve(profile, directory, root)
        check_boundary(definition['entry'])
        return 'pure Protocol; transport managed by shared host'

    check("adapter", adapter)
    checked = check("capture and replay", lambda: sorted(replay.verify(profile, directory, root)))
    if checked is not None:
        missing = sorted(replay.required_cases(profile) - set(checked))
        result["checks"].append({"check": "capability evidence", "passed": not missing,
                                 "detail": "missing cases: " + ", ".join(missing) if missing else "all declared controls have asserted replies"})
    if checked is not None:
        check("command round trips", lambda: contracts.roundtrips(profile, directory, root))
        check("coalesced delivery", lambda: contracts.coalesced(profile, directory, root))
        check("session isolation", lambda: contracts.isolation(profile, directory, root))
    check("fault scenarios", lambda: fault_tests(directory))

    def hardware():
        report = profiles.read_json(directory / "hardware-check.json")
        if report.get("apiVersion") != 1 or report.get("device") != profile["id"] or report.get("owner") != profile["owner"]:
            raise ValueError("hardware report must identify this device and owner")
        if report.get("implementation") != profiles.implementation_hash(profile, directory, root):
            raise ValueError("hardware report is stale for this executable/profile; rerun live check")
        if report.get("scope") != "adapter" or report.get("passed") is not True or not live.initial_ready(profile, report.get("initial", {})):
            raise ValueError("owner adapter check failed or has no initial state")
        if not report.get("restoration") or not all(r.get("passed") is True for r in report["restoration"]):
            raise ValueError("initial settings were not confirmed restored")
        initial = report['initial']['values']
        restored = {}
        from .runner import parse_command
        for item in report['restoration']:
            command = parse_command(item.get('command', ''))
            if not command:
                raise ValueError('restoration needs exact host command')
            key, value = command
            if key not in initial or type(value) is not type(initial[key]) or value != initial[key] or item.get('reported', {}).get('values', {}).get(key) != initial[key]:
                raise ValueError('restoration does not confirm initial setting: ' + key)
            restored[key] = value
        required_fields = {key for key, spec in profile['capabilities'].items() if not spec.get('readOnly')}
        if set(restored) != required_fields:
            raise ValueError('restoration misses declared writable settings')
        required = {case for case, *_ in live.controls(profile)}
        observed = set()
        for item in report.get("checks", []):
            if item.get("passed") is True:
                replay.validate_case(item["case"], item["reported"], profile)
                observed.add(item["case"])
        if required - observed:
            raise ValueError("hardware report misses controls: " + ", ".join(sorted(required - observed)))
        return "owner adapter check passed; manual integration evidence checked separately"

    check("owner hardware", hardware)

    def manual():
        report = profiles.read_json(directory / "owner-checks.json")
        if report.get("owner") != profile["owner"] or report.get("implementation") != profiles.implementation_hash(profile, directory, root):
            raise ValueError("owner checks must name the owner and current implementation hash")
        required = {"shell-integration", "reconnect"}
        caps = profile["capabilities"]
        if "wear.detected" in caps:
            required.update(("wear.detected:true", "wear.detected:false"))
        if profile.get("batterySource") != "bridge":
            required.update("battery:" + p for p in caps.get("battery", {}).get("parts", []))
        checks = report.get("checks", {})
        for key in sorted(required):
            record = checks.get(key, {})
            if record.get("status") != "passed" or not isinstance(record.get("evidence"), str) or not record["evidence"].strip():
                raise ValueError("missing owner observation: " + key)
        for key in ("peer-isolation", "charging", "acoustics"):
            record = checks.get(key, {})
            if record.get("status") not in ("passed", "untested", "not-applicable") or not record.get("evidence"):
                raise ValueError("state result or limit for " + key)
        limits = [key for key, item in checks.items() if item.get("status") != "passed"]
        return "owner attestation; limits: " + (", ".join(limits) or "none declared")

    check("owner integration and limits", manual)

    def screenshot():
        image = directory / "screenshot.png"
        if not valid_png(image.read_bytes()):
            raise ValueError("screenshot.png must be the owner's real panel screenshot")
        return "PNG present; visual/hardware authenticity remains an owner claim"

    check("gallery", screenshot)
    def protocol_notes():
        notes = (directory / "protocol.md").read_text().strip()
        if not notes or "Describe observed requests" in notes:
            raise ValueError("describe observed protocol and limits in protocol.md")
        return "protocol notes present"

    check("protocol notes", protocol_notes)
    result["passed"] = all(item["passed"] for item in result["checks"])
    return feedback.decorate(result, directory, root)


def immutable(root=profiles.ROOT, base=None):
    if not base:
        for candidate in ('origin/main', 'main'):
            if subprocess.run(['git', 'rev-parse', '--verify', candidate], cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                base = candidate
                break
        if not base:
            raise ValueError('no ownership baseline; supply --base')
    since = subprocess.check_output(["git", "merge-base", base, "HEAD"], cwd=root, text=True).strip()
    changed = subprocess.check_output(["git", "diff", "--diff-filter=MDR", "--name-only", since, "--", "devices", "tests/pins", "docs/captures"],
                                      cwd=root, text=True).splitlines()
    protected = [p for p in changed if len(p.split("/")) >= 3]
    if protected:
        error = ValueError("existing owner evidence changed: " + ", ".join(protected))
        error.filename = protected[0]
        raise error
    return "existing owner packages, pins and captures unchanged"


def markdown(reports):
    import html
    def cell(value):
        return html.escape(str(value)).replace('|', '&#124;').replace('\r', ' ').replace('\n', '<br>')
    lines = ["| Device | Check | Result | File | Detail / next step |", "|:--|:--|:--|:--|:--|"]
    for report in reports:
        for item in report["checks"]:
            detail = cell(item['detail'])
            if not item['passed']:
                detail += '<br>Fix: ' + cell(item.get('fix', 'Resolve the reported problem.'))
                detail += '<br>Run: <code>' + cell(item.get('reproduce', 'tools/check')) + '</code>'
            lines.append('| %s | %s | %s | %s | %s |' % (cell(report['device']), cell(item['check']), 'OK' if item['passed'] else 'FAILED', cell(item.get('file', '')), detail))
    return '\n'.join(lines)
