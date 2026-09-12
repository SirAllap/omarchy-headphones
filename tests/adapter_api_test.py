"""API regression fixtures are synthetic; they are never new-model evidence."""
import copy
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from adapter_api import contribution, live, profiles, replay, runtime


def profile(name="nothing"):
    uuids = profiles.BUILTINS[name].get("uuids", [])
    if name == "soundcore":
        uuids = ["0cf12d31-fac3-4553-bd80-d6832e7abcde"]
    if name == "jbl":
        uuids = ["df21fe2c-2515-4fdb-8886-f12c4d67927c"]
    params = {"nothing": {"channels": [28]}, "bose": {"channels": [8]}, "sony": {"wear": False},
              "soundcore": {"offset": 71, "query": False}}.get(name, {})
    result = {"apiVersion": 1, "id": "api-fixture", "model": "API Fixture", "owner": "fixture-owner",
              "match": {"names": ["API Fixture"], "uuids": uuids},
              "adapter": {"builtin": name, "transport": "ble" if name == "jbl" else "classic", "parameters": params},
              "capabilities": {"modes": ["off", "anc", "ambient"]}}
    if name == "jbl":
        result["match"]["modelId"] = "123456"
    return result


def context(p=None, directory=None):
    p = p or profile()
    return runtime.Context(p, directory or profiles.ROOT / "devices/api-fixture", "AA:BB:CC:DD:EE:FF",
                           p["match"]["names"][0], p["match"]["uuids"], "11:22:33:44:55:66",
                           p["match"].get("modelId", ""))


def metadata(p):
    return {"apiVersion": 1, "device": p["id"], "address": "AA:BB:CC:DD:EE:FF",
            "name": p["match"]["names"][0], "uuids": p["match"]["uuids"],
            "modelId": p["match"].get("modelId", ""), "boundary": "stream"}


class Profiles(unittest.TestCase):
    def test_every_builtin_has_a_valid_explicit_profile(self):
        for name in profiles.BUILTINS:
            with self.subTest(name=name):
                profiles.validate(profile(name))

    def test_missing_or_borrowed_parameters_do_not_default_to_another_model(self):
        for name in ("nothing", "bose", "sony", "soundcore"):
            p = profile(name)
            p["adapter"]["parameters"] = {}
            with self.subTest(name=name), self.assertRaises(ValueError):
                profiles.validate(p)

    def test_identity_uses_reported_name_and_complete_uuid_list(self):
        text = "Device AA:BB:CC:DD:EE:FF (public)\n\tAlias: My nickname\n\tName: API Fixture\n\tUUID: Vendor (AEAC4A03-DFF5-498F-843A-34487CF133EB)\n"
        record = profiles.identity(text)
        self.assertEqual(record["name"], "API Fixture")
        self.assertEqual(record["uuids"], profile()["match"]["uuids"])
        self.assertTrue(profiles.match_profile(profile(), record["name"], record["uuids"]))
        self.assertFalse(profiles.match_profile(profile(), "My nickname", record["uuids"]))
        self.assertFalse(profiles.match_profile(profile(), "API Fixture", []))

    def test_invalid_identity_parameters_and_capabilities_are_rejected(self):
        edits = [lambda p: p.update(apiVersion=2), lambda p: p.update(id="../escape"),
                 lambda p: p["match"].update(names=[]), lambda p: p["match"].update(uuids=[]),
                 lambda p: p["adapter"].update(module="adapter.py"),
                 lambda p: p["adapter"]["parameters"].update(channels=[31]),
                 lambda p: p["capabilities"].update(modes=["reboot"]),
                 lambda p: p["capabilities"].update(latency="false")]
        for edit in edits:
            p = profile()
            edit(p)
            with self.subTest(profile=p), self.assertRaises(ValueError):
                profiles.validate(p)

    def test_existing_owner_model_cannot_be_claimed_by_a_new_package(self):
        for name, reported in (("sony", "WH-CH720N"), ("nothing", "Nothing Ear (a)"),
                               ("nothing", "CMF Headphone Pro"), ("soundcore", "Space 2")):
            p = profile(name)
            p["match"]["names"] = [reported]
            with self.subTest(reported=reported), self.assertRaises(ValueError):
                contribution.no_legacy_claim(p, profiles.ROOT)

    def test_artifact_cannot_escape_through_relative_path_or_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "device"
            directory.mkdir()
            (directory / "outside").symlink_to(Path(tmp))
            for path in ("../file", "/etc/passwd", "outside/file"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    profiles.inside(directory, path)


class BuiltinAdapters(unittest.TestCase):
    def test_jbl_client_boundary_keeps_startup_and_exact_control_bytes(self):
        p = profile("jbl")
        session = replay.BuiltinSession(context(p))
        try:
            session.start()
            session.receive("Connecting to device... Done")
            session.receive("GATT discovery procedures complete")
            session.wait_sent(1)
            session.receive("Registered notify handler")
            session.wait_sent(3)
            session.receive("Handle Value Not/Ind: 0x000c - (10 data bytes): aa 91 07 12 01 01 02 00 03 00")
            session.session.do_wait_start()
            self.assertEqual(session.lines[-1]["mode"], "anc")
            before = copy.deepcopy(session.lines)
            session.command("set off")
            self.assertEqual(session.lines, before)
            self.assertEqual(session.sent[-1], "write-value 0x0010 0xaa 0x91 0x07 0x10 0x01 0x00 0x02 0x00 0x03 0x00")
        finally:
            session.close()

    def test_nothing_silence_is_detected_by_the_actual_run_loop(self):
        session = replay.ClockedLoopSession(context(profile()))
        try:
            session.start()
            self.assertEqual(session.bridge.exit_code, None)
            milliseconds = int((session.module.INFO_TIMEOUT + session.module.ANSWER_TIMEOUT + 1) * 1000)
            session.advance(milliseconds)
            self.assertTrue(session.finished)
            self.assertEqual(session.bridge.exit_code, 3)
            self.assertFalse(session.lines[-1]["modes"])
        finally:
            session.close()

    def test_bose_real_init_validator_retains_joined_frames_and_scheduled_readback(self):
        p = profile("bose")
        p["capabilities"]["modes"] = ["anc", "ambient"]
        session = replay.ClockedLoopSession(context(p))
        try:
            session.start()
            self.assertEqual(session.sent, [bytes.fromhex("00 01 01 00")])
            # Canonical QC45 bytes test API transport handling, not a new model.
            session.receive(bytes.fromhex("00 01 03 05 31 2e 31 2e 30 1f 03 03 01 01"))
            self.assertEqual(session.lines[-1]["mode"], "ambient")
            before = copy.deepcopy(session.lines)
            session.command("set anc")
            self.assertEqual(session.lines, before)
            sent = len(session.sent)
            session.advance(int(session.module.READBACK_DELAY * 1000))
            self.assertEqual(session.sent[sent:], [session.module.CURRENT])
            session.receive(bytes.fromhex("1f 03 03 01 00"))
            self.assertEqual(session.lines[-1]["mode"], "anc")
        finally:
            session.close()

    def test_model_configuration_is_private_and_commands_are_capability_gated(self):
        from tests import harness
        original = harness.load_bridge("nothing-bridge")
        before = copy.deepcopy(original.MODELS)
        ctx = context()
        configured = runtime.configure(harness.load_bridge("nothing-bridge"), ctx)
        device = configured.Bridge(ctx.address, ctx.name)
        self.assertEqual(device.model["channels"], [28])
        self.assertEqual(original.MODELS, before)
        self.assertEqual(original.model_for("Ear (a)")["channels"], (15,))
        device.mode = "off"
        ctx.output = lambda _state: None
        ctx.emit({"modes": True, "mode": "off"})
        sent = []
        device.sock = types.SimpleNamespace(sendall=sent.append)
        device.command("latency on")
        device.command("set talkthru")
        self.assertEqual(sent, [])
        device.command("set anc")
        self.assertTrue(sent)

    def test_reported_capabilities_are_intersection_with_profile(self):
        p = profile()
        state = {"modes": True, "mode": "off", "available": ["off", "anc", "ambient", "talkthru"],
                 "latency": True, "worn": True, "ancLevels": ["high"], "ancLevel": "high",
                 "battery": {"left": 80}, "level": 4, "voice": True}
        self.assertEqual(runtime.state_for(p, state), {"modes": True, "mode": "off", "available": ["off", "anc", "ambient"]})
        self.assertIn("latency", state)  # The bridge's own state was not mutated.

    def test_each_stream_adapter_replays_startup_without_hardware(self):
        for name in ("sony", "nothing", "bose", "samsung", "soundcore", "oppo", "xiaomi"):
            with self.subTest(name=name):
                p = profile(name)
                session = replay.factory(p, profiles.ROOT / "devices/api-fixture", metadata(p))
                try:
                    session.start()
                    self.assertTrue(session.sent, "startup must actually query")
                    self.assertTrue(all(isinstance(data, bytes) for data in session.sent))
                    before = list(session.sent)
                    session.command("set anc")
                    self.assertEqual(session.sent, before, "no controls before a device answer")
                finally:
                    session.close()

    def test_sony_raw_canonical_reply_uses_runtime_configuration(self):
        # Owner-observed reference bytes test API plumbing only, not another model.
        reference = profiles.read_json(profiles.ROOT / "tests/fixtures/canonical.json")["sony"]
        session = replay.BuiltinSession(context(profile("sony")))
        try:
            session.start()
            for key in ("handshake", "initial", "off", "anc", "ambient"):
                raw = bytes.fromhex(reference["rx"][key]["hex"])
                for byte in raw:
                    session.receive(bytes([byte]))
            self.assertEqual(session.lines[-1]["mode"], "ambient")
            self.assertNotIn("level", session.lines[-1])
            self.assertFalse(session.bridge.model["wear"])
        finally:
            session.close()


class Capture(unittest.TestCase):
    def test_recorder_preserves_chunks_directions_and_monotonic_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capture.jsonl"
            recorder = runtime.Recorder(path, metadata(profile()))
            recorder.record("rx", b"\x55\x60")
            recorder.record("rx", b"\x01")
            recorder.record("tx", b"\xff")
            recorder.close()
            events = replay.capture(path, profile())
            self.assertEqual([replay.data(e) for e in list(events.values())[1:]], [b"\x55\x60", b"\x01", b"\xff"])
            with self.assertRaises(FileExistsError):
                runtime.Recorder(path, {})

    def test_forged_identity_and_synthetic_evidence_are_rejected(self):
        for change in ({"name": "Unrelated device"}, {"synthetic": True}):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "capture.jsonl"
                recorder = runtime.Recorder(path, {**metadata(profile()), **change})
                recorder.close()
                with self.assertRaises(ValueError):
                    replay.capture(path, profile())

    def test_wrong_direction_cannot_be_used_as_device_evidence(self):
        fake = FakeSession()
        events = {"1": {"data": metadata(profile())}, "2": {"direction": "tx", "encoding": "hex", "data": "aa"}}
        with patch.object(replay, "factory", return_value=fake), self.assertRaisesRegex(ValueError, "RX references"):
            replay.execute(profile(), Path("."), events, {"apiVersion": 1, "steps": [{"rx": "2"}]})
        self.assertTrue(fake.closed)

    def test_replay_does_not_accept_unreviewed_or_optimistic_state(self):
        events = {"1": {"data": metadata(profile())}}
        for step in ({"expect": None}, {"command": "set anc"}):
            fake = FakeSession(optimistic=True)
            with patch.object(replay, "factory", return_value=fake), self.assertRaises((ValueError, AssertionError)):
                replay.execute(profile(), Path("."), events, {"apiVersion": 1, "steps": [step]})

    def test_replay_detects_wrong_tx_and_all_fragment_boundaries(self):
        p = profile()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            recorder = runtime.Recorder(directory / "capture.jsonl", metadata(p))
            recorder.record("tx", b"\xaa")
            recorder.record("rx", b"\x01\x02\x03")
            recorder.close()
            spec = {"apiVersion": 1, "steps": [{"start": True}, {"rx": "3"}, {"tx": ["2"]},
                    {"expect": {"modes": True, "mode": "off"}, "case": "mode:off"}]}
            (directory / "session.json").write_text(json.dumps(spec))
            made = []

            def factory(*args):
                session = FakeSession()
                made.append(session)
                return session

            with patch.object(replay, "factory", side_effect=factory):
                self.assertEqual(replay.verify(p, directory), {"mode:off"})
            self.assertEqual(len(made), 3)
            self.assertEqual(made[1].received, [b"\x01", b"\x02\x03"])
            self.assertEqual(made[2].received, [b"\x01\x02", b"\x03"])
            events = replay.capture(directory / "capture.jsonl", p)
            events["2"]["data"] = "bb"
            with patch.object(replay, "factory", return_value=FakeSession()), self.assertRaises(AssertionError):
                replay.execute(p, directory, events, spec)


class FakeSession:
    def __init__(self, optimistic=False):
        self.lines = []
        self.sent = []
        self.received = []
        self.closed = False
        self.optimistic = optimistic

    def start(self):
        self.sent.append(b"\xaa")

    def receive(self, data):
        self.received.append(data)
        if b"".join(self.received) == b"\x01\x02\x03":
            self.lines.append({"modes": True, "mode": "off"})

    def command(self, line):
        if self.optimistic:
            self.lines.append({"modes": True, "mode": "anc"})

    def wait_sent(self, count):
        pass

    def close(self):
        self.closed = True


class FakeLiveClient:
    def __init__(self, fail=None, ready=True):
        self.commands = []
        self.fail = fail
        self.ready = ready
        self.closed = False
        self.state = {"modes": True, "mode": "ambient", "level": 5, "voice": True, "ancLevel": "high", "latency": False}

    def wait(self, predicate):
        if not self.ready or not predicate(self.state):
            raise TimeoutError("unknown initial state")
        return copy.deepcopy(self.state)

    def set(self, command, field, value):
        self.commands.append(command)
        if self.fail and self.fail(command):
            raise RuntimeError("link lost")
        self.state[field] = value
        return copy.deepcopy(self.state)

    def close(self):
        self.closed = True


class LiveChecks(unittest.TestCase):
    def run_check(self, client, p=None):
        output = io.StringIO()
        with patch.object(profiles, "implementation_hash", return_value="test-hash"):
            report = live.run(p or profile(), Path("."), client, output)
        self.assertEqual(report, json.loads(output.getvalue()))
        self.assertTrue(client.closed)
        return report

    def test_unknown_initial_state_sends_no_controls(self):
        client = FakeLiveClient(ready=False)
        report = self.run_check(client)
        self.assertFalse(report["passed"])
        self.assertEqual(client.commands, [])
        self.assertIn("no controls", report["restoration"][0]["error"])

    def test_failure_still_restores_and_records_original_state(self):
        client = FakeLiveClient(fail=lambda cmd: cmd == "set anc")
        report = self.run_check(client)
        self.assertFalse(report["passed"])
        self.assertEqual(client.commands[-1], "set ambient")
        self.assertTrue(report["restoration"][0]["passed"])

    def test_restore_failure_marks_report_failed_and_does_not_skip_other_controls(self):
        p = profile("sony")
        p["capabilities"].update(ambient={"min": 0, "max": 20, "voice": "Focus", "voiceCommand": "voice"},
                                 ancLevels=["low", "high"], latency=True)
        client = FakeLiveClient(fail=lambda cmd: cmd == "level 5")
        report = self.run_check(client, p)
        self.assertFalse(report["passed"])
        self.assertEqual([r["command"] for r in report["restoration"]],
                         ["level high", "level 5", "voice on", "latency off", "set ambient"])
        self.assertTrue(report["restoration"][-1]["passed"])

    def test_success_reports_unperformed_hardware_checks_as_untested(self):
        report = self.run_check(FakeLiveClient())
        self.assertTrue(report["passed"])
        self.assertIn("peer-isolation", report["untested"])
        self.assertIn("shell-integration", report["untested"])


class Contribution(unittest.TestCase):
    def test_registry_rejects_ambiguous_device_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for slug in ("first-fixture", "second-fixture"):
                directory = root / "devices" / slug
                directory.mkdir(parents=True)
                p = profile()
                p["id"] = slug
                (directory / "device.json").write_text(json.dumps(p))
            with self.assertRaisesRegex(ValueError, "overlapping"):
                profiles.registry(root)

    def test_sync_is_deterministic_and_check_detects_stale_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("Model.js", "README.md"):
                shutil.copy(profiles.ROOT / name, root / name)
            directory = root / "devices/api-fixture"
            directory.mkdir(parents=True)
            (directory / "device.json").write_text(json.dumps(profile()))
            with self.assertRaisesRegex(ValueError, "stale"):
                contribution.generate(root)
            self.assertEqual(contribution.generate(root, write=True), ["Model.js", "README.md"])
            self.assertEqual(contribution.generate(root), [])
            self.assertEqual(contribution.generate(root, write=True), [])
            self.assertIn("device-adapter", (root / "Model.js").read_text())
            self.assertIn("devices/api-fixture/screenshot.png", (root / "README.md").read_text())

    def test_missing_artifacts_are_reported_individually(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "api-fixture"
            directory.mkdir()
            (directory / "device.json").write_text(json.dumps(profile()))
            report = contribution.readiness(directory)
            self.assertFalse(report["passed"])
            missing = {c["check"] for c in report["checks"] if not c["passed"]}
            self.assertTrue({"capture and replay", "owner hardware", "gallery", "fault scenarios"}.issubset(missing))

    def test_skipped_fault_scenarios_cannot_pass_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source = "import unittest\nclass Faults(unittest.TestCase):\n"
            for case in contribution.FAULTS:
                source += " def test_" + case + "(self): self.skipTest('not yet observed')\n"
            (directory / "test_adapter.py").write_text(source)
            with self.assertRaisesRegex(ValueError, "incomplete"):
                contribution.fault_tests(directory)

    def test_launcher_rejects_bad_input_without_importing_adapter(self):
        command = [sys.executable, str(profiles.ROOT / "device-adapter"), "../escape", "bad", "name", "[]", "", ""]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 4)
        self.assertFalse(json.loads(result.stdout)["modes"])
        self.assertEqual(result.stderr, "")


LOCAL_ADAPTER = '''"""Synthetic subprocess adapter for API tests; no Bluetooth I/O."""
import sys
from adapter_api import runtime
API_VERSION = 1

def run(context):
    state = "off"
    context.record("tx", b"\\xaa")
    context.record("rx", b"\\x00")
    context.emit({"modes": True, "mode": state})
    for line in sys.stdin:
        line = line.strip()
        context.record("command", line)
        if not context.mode_seen or not runtime.allowed(context.profile, line):
            continue
        state = line.split()[1]
        wire = bytes([{"off": 0, "anc": 1, "ambient": 2}[state]])
        context.record("tx", wire)
        context.record("rx", wire)
        context.emit({"modes": True, "mode": state})
    return 0

class Session:
    def __init__(self, context):
        self.context = context
        self.lines = []
        self.sent = []
        context.output = self.lines.append
    def start(self):
        self.sent.append(b"\\xaa")
    def receive(self, data):
        for byte in data:
            self.context.emit({"modes": True, "mode": ["off", "anc", "ambient"][byte]})
    def command(self, line):
        if self.context.mode_seen and runtime.allowed(self.context.profile, line):
            self.sent.append(bytes([{"off": 0, "anc": 1, "ambient": 2}[line.split()[1]]]))
    def wait_sent(self, count):
        pass
    def close(self):
        pass

def replay(context):
    return Session(context)
'''


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="omaphones-api-e2e-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        shutil.copytree(profiles.ROOT / "adapter_api", self.root / "adapter_api")
        (self.root / "tools").mkdir()
        for name in ("device-adapter", "Model.js", "README.md", "DeviceFollower.qml", "Service.qml", "Panel.qml", "gfps-reader", "tools/add-device"):
            shutil.copy(profiles.ROOT / name, self.root / name)
        self.directory = self.root / "devices/api-fixture"
        self.directory.mkdir(parents=True)
        self.p = profile()
        self.p["adapter"] = {"module": "adapter.py", "transport": "classic", "parameters": {}}
        (self.directory / "device.json").write_text(json.dumps(self.p))
        (self.directory / "adapter.py").write_text(LOCAL_ADAPTER)

    def test_real_launcher_capture_live_restoration_and_offline_replay(self):
        target = self.directory / "capture.jsonl"
        command = [sys.executable, str(self.root / "device-adapter"), self.p["id"], "AA:BB:CC:DD:EE:FF",
                   "API Fixture", json.dumps(self.p["match"]["uuids"]), "", "", "--capture", str(target)]
        client = live.Client(command)
        output = io.StringIO()
        report = live.run(self.p, self.directory, client, output, self.root)
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["restoration"][-1]["reported"]["mode"], "off")
        events = replay.capture(target, self.p)
        self.assertTrue(any(e["direction"] == "command" for e in events.values()))
        command = [sys.executable, str(self.root / "tools/add-device"), "session", self.p["id"]]
        generated = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(generated.returncode, 0, generated.stderr)
        spec = profiles.read_json(self.directory / "session.json")
        self.assertTrue(any(s.get("expect", "absent") is None for s in spec["steps"]))
        with self.assertRaisesRegex(ValueError, "reviewed"):
            replay.verify(self.p, self.directory)
        # These expectations come from the test's independent three-byte protocol.
        current = "off"
        for step in spec["steps"]:
            if "rx" in step:
                current = ["off", "anc", "ambient"][replay.data(events[step["rx"]])[0]]
            if "expect" in step:
                step["expect"] = {"modes": True, "mode": current, "available": ["off", "anc", "ambient"]}
                step["case"] = "mode:" + current
        (self.directory / "session.json").write_text(json.dumps(spec))
        self.assertEqual(replay.verify(self.p, self.directory), {"mode:off", "mode:anc", "mode:ambient"})
        self.assertEqual(report["implementation"], profiles.implementation_hash(self.p, self.directory, self.root))
        with (self.directory / "adapter.py").open("a") as handle:
            handle.write("\n# Changed executable revision\n")
        self.assertNotEqual(report["implementation"], profiles.implementation_hash(self.p, self.directory, self.root))

    def test_cli_init_from_identity_stays_in_one_new_package_and_refuses_overwrite(self):
        source = self.root / "identity.txt"
        source.write_text("Device AA:BB:CC:DD:EE:FF (public)\n\tName: Another API Fixture\n\tUUID: Vendor (aeac4a03-dff5-498f-843a-34487cf133eb)\n")
        command = [sys.executable, str(self.root / "tools/add-device"), "init", "another-fixture", "--owner", "fixture-owner", "--info", str(source)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        directory = self.root / "devices/another-fixture"
        created = profiles.read_json(directory / "device.json")
        self.assertEqual(created["adapter"]["builtin"], "nothing")
        self.assertIsNone(created["adapter"]["parameters"]["channels"])
        self.assertFalse((directory / "capture.jsonl").exists())
        repeated = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(repeated.returncode, 0)
        self.assertEqual(created, profiles.read_json(directory / "device.json"))

    def test_cli_check_returns_nonzero_for_an_incomplete_package(self):
        summary = self.root / "summary.md"
        command = [sys.executable, str(self.root / "tools/add-device"), "check", self.p["id"], "--json", "--summary", str(summary)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report[0]["passed"])
        self.assertTrue(any(c["check"] == "owner hardware" and not c["passed"] for c in report[0]["checks"]))
        self.assertEqual(summary.read_text(), contribution.markdown(report) + "\n")


if __name__ == "__main__":
    unittest.main()
