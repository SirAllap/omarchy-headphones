"""Synthetic SDK fixtures, never owner hardware evidence."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from omaphones import devices, evidence, live, contribution
from omaphones.api import Protocol, Report
from omaphones.checking import check_boundary
from omaphones.recording import Recorder
from omaphones.registry import ROOT, get_adapter, instantiate
from omaphones.scaffold import initialize
from omaphones.testing import Replay
from omaphones.runner import parse_command

UUID = '10000000-0000-0000-0000-000000000001'
INFO = 'Device AA:BB:CC:DD:EE:FF\n\tName: Synthetic Headset\n\tUUID: Test (' + UUID + ')\n'
CAPS = {'noise.mode': {'type': 'enum', 'values': ['off', 'anc']}}
SOURCE = '''from omaphones.api import Protocol
class Adapter(Protocol):
    def __init__(self, model=None):
        super().__init__(model)
        self.buffer = b""
    def connected(self):
        self.write(b"query\\n")
    def received(self, data):
        self.buffer += data
        while b"\\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\\n", 1)
            if line in (b"off", b"anc"):
                self.report({"noise.mode": line.decode()}, {"noise.mode": {"values": ["off", "anc"]}})
    def command(self, control, value):
        self.write(value.encode() + b"\\n")
'''


class Packages(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('omaphones', 'adapters'):
            shutil.copytree(ROOT / name, self.root / name)
        for name in ('Model.js', 'README.md', 'omaphones-device', 'DeviceFollower.qml', 'Service.qml', 'Panel.qml', 'gfps-reader'):
            shutil.copy(ROOT / name, self.root / name)
        for path in ROOT.glob('*-bridge'):
            shutil.copy(path, self.root / path.name)
        (self.root/'tools').mkdir()
        shutil.copy(ROOT/'tools/add-device', self.root/'tools/add-device')
        self.directory = initialize('synthetic', 'test-owner', INFO, root=self.root)
        self.profile = json.loads((self.directory/'device.json').read_text())
        self.profile['capabilities'] = deepcopy(CAPS)
        self.profile['adapter']['transport']['uuidPreference'] = [UUID]
        (self.directory/'protocol.py').write_text(SOURCE)
        self.save()

    def save(self):
        (self.directory/'device.json').write_text(json.dumps(self.profile))

    def test_drafts_inert_and_overwrite_refused(self):
        self.assertEqual(devices.packages(self.root), [])
        with self.assertRaises(FileExistsError):
            initialize('synthetic', 'test-owner', INFO, root=self.root)
        with patch('omaphones.scaffold.installed_root', return_value=self.root):
            with self.assertRaisesRegex(ValueError, 'isolated'):
                initialize('other', 'test-owner', INFO, root=self.root)
        self.assertFalse((self.root/'devices/other').exists())

    def test_empty_evidence_and_exemption_rejected(self):
        (self.directory/'capture.jsonl').write_text('')
        with self.assertRaisesRegex(ValueError, 'metadata'):
            evidence.verify(self.profile, self.directory, self.root)
        self.profile['evidence'] = 'existing'
        with self.assertRaisesRegex(ValueError, 'exemptions'):
            devices.validate(self.profile)

    def test_exact_identity_not_just_equal_parameters(self):
        context = devices.identity(INFO)
        devices.open_protocol(self.profile, self.directory, context, self.root)
        for changed in ({'name': 'Other'}, {'uuids': []}):
            with self.assertRaisesRegex(ValueError, 'exact model'):
                devices.open_protocol(self.profile, self.directory, {**context, **changed}, self.root)
        self.profile['match']['modelId'] = '123abc'
        self.assertFalse(devices.matches(self.profile, context))
        self.assertTrue(devices.matches(self.profile, {**context, 'modelId': '123ABC'}))

    def test_active_overlap_and_owner_collision_rejected(self):
        self.profile['status'] = 'active'; self.save()
        peer = self.root/'devices/peer'; peer.mkdir()
        (peer/'device.json').write_text(json.dumps({**self.profile, 'id': 'peer'}))
        with self.assertRaisesRegex(ValueError, 'overlapping'):
            devices.packages(self.root)
        shutil.rmtree(peer)
        shutil.copytree(ROOT/'tests/pins', self.root/'tests/pins')
        for name in ('WH-CH720N', 'CMF Headphone Pro', 'Bose QC45'):
            self.profile['match']['names'] = [name]
            with self.assertRaisesRegex(ValueError, 'already supported'):
                devices.no_legacy_claim(self.profile, self.root)

    def test_model_transport_override_is_explicit_and_isolated(self):
        self.profile['adapter'] = {'id': 'nothing', 'parameters': {'defaultAncLevel': 'adaptive'}, 'transport': {'channels': [28]}}
        definition = devices.resolve(self.profile, self.directory, self.root)
        self.assertEqual(definition['transport']['channels'], [28])
        self.assertEqual(get_adapter('nothing', self.root)['transport']['channels'], [15, 28])
        for override in ({}, {'channels': [31]}, {'kind': 'ble-gatt'}):
            self.profile['adapter']['transport'] = override
            with self.assertRaises(ValueError):
                devices.resolve(self.profile, self.directory, self.root)

    def test_protocol_boundary_has_no_platform_imports(self):
        for source in ('import socket', 'from os import read', 'open("state")', '__import__("os")'):
            (self.directory/'protocol.py').write_text(source)
            with self.assertRaises(ValueError):
                check_boundary(self.directory/'protocol.py')

    def test_hash_ignores_activation_and_unrelated_registry_but_covers_runtime(self):
        original = devices.implementation_hash(self.profile, self.directory, self.root)
        self.profile['status'] = 'active'
        self.assertEqual(original, devices.implementation_hash(self.profile, self.directory, self.root))
        (self.root/'omaphones/session.py').write_text('# changed runtime')
        self.assertNotEqual(original, devices.implementation_hash(self.profile, self.directory, self.root))

    def recorded(self):
        context = devices.identity(INFO)
        path = self.directory/'capture.jsonl'
        recorder = Recorder(path, {'apiVersion': 1, 'device': 'synthetic', 'owner': 'test-owner', 'context': context, 'boundary': 'stream'})
        replay = Replay(devices.open_protocol(self.profile, self.directory, context, self.root), limits=CAPS)
        replay.session.recorder = recorder
        write = replay.write
        def record_write(data):
            write(data); recorder.record('tx', data)
        replay.write = record_write
        replay.event('connected'); replay.receive(b'off\n')
        replay.command('noise.mode', 'anc'); replay.receive(b'anc\n')
        replay.receive(b'anc\n')
        replay.command('noise.mode', 'talkthru')
        replay.receive(b'off\n')
        recorder.close()
        events = evidence.capture(path, self.profile)
        # Independently declared state, not copied from protocol output.
        snapshot = lambda value: {'apiVersion': 1, 'capabilities': CAPS, 'values': {'noise.mode': value}}
        steps, sent, labels = [], [], iter([('off', 'initial'), ('anc', 'noise.mode:anc'), ('anc', 'repeated'), ('off', 'external-change')])
        for ref, event in events.items():
            if event['direction'] == 'tx': sent.append(ref)
            elif event['direction'] in evidence.INPUTS:
                steps.append({'feed': ref})
                if event['direction'] == 'rx':
                    value, label = next(labels)
                    steps += [{'expect': snapshot(value), 'case': label}, {'expect': snapshot(value), 'case': 'noise.mode:' + value}]
                elif event['direction'] == 'command' and event['data']['value'] == 'talkthru':
                    steps.append({'expect': snapshot('anc'), 'case': 'unsupported-command'})
        steps.append({'sent': sent})
        (self.directory/'session.json').write_text(json.dumps({'apiVersion': 1, 'steps': steps}))
        return events

    def test_same_session_capture_replay_all_splits_and_exact_tx(self):
        events = self.recorded()
        self.assertEqual(evidence.verify(self.profile, self.directory, self.root), evidence.required_cases(self.profile))
        path = self.directory/'session.json'
        session = json.loads(path.read_text())
        session['steps'][-1]['sent'].reverse(); path.write_text(json.dumps(session))
        with self.assertRaises(AssertionError):
            evidence.verify(self.profile, self.directory, self.root)

    def test_capture_cannot_skip_inputs_or_fake_identity(self):
        self.recorded()
        path = self.directory/'session.json'; session = json.loads(path.read_text())
        session['steps'].pop(0); path.write_text(json.dumps(session))
        with self.assertRaisesRegex(ValueError, 'preserve'):
            evidence.verify(self.profile, self.directory, self.root)
        events = [json.loads(line) for line in (self.directory/'capture.jsonl').read_text().splitlines()]
        events[0]['data']['context']['name'] = 'Other'
        (self.directory/'capture.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
        with self.assertRaisesRegex(ValueError, 'exact model'):
            evidence.capture(self.directory/'capture.jsonl', self.profile)

    def test_readiness_fails_without_hardware_and_model_faults(self):
        self.recorded()
        report = contribution.readiness(self.directory, self.root)
        failed = {c['check'] for c in report['checks'] if not c['passed']}
        self.assertIn('owner hardware', failed)
        self.assertIn('fault scenarios', failed)
        self.assertNotIn('capture and replay', failed)

    def test_live_controls_parse_as_host_commands_and_restore_mode_last(self):
        for _case, command, key, value in live.controls(self.profile):
            self.assertEqual(parse_command(command), (key, value))
        state = {'apiVersion': 1, 'values': {'noise.mode': 'off'}, 'capabilities': CAPS}
        self.assertTrue(live.initial_ready(self.profile, state))
        self.assertFalse(live.initial_ready(self.profile, {}))
        self.assertEqual(live.restore_actions(self.profile, state)[-1][1:], ('noise.mode', 'off'))

    def test_command_cannot_publish_optimistic_state(self):
        class Optimistic(Protocol):
            def received(self, data): self.report({'noise.mode': 'off'}, CAPS)
            def command(self, control, value): self.report({control: value})
        replay = Replay(Optimistic()); replay.receive(b'fixture'); replay.command('noise.mode', 'anc')
        self.assertEqual(replay.session.state.values, {'noise.mode': 'off'})
        self.assertEqual(replay.session.exit_code, 1)

    def test_undeclared_controls_and_battery_parts_do_not_escape_limits(self):
        from omaphones.state import State
        limits = {**CAPS, 'battery': {'readOnly': True, 'parts': ['left']}}
        state = State(limits)
        state.apply(Report({'noise.mode': 'off', 'wear.detected': True, 'battery': {'left': 10, 'right': 90, 'charging': ['right']}},
                           {**CAPS, 'wear.detected': {'type': 'boolean', 'readOnly': True}, 'battery': {'readOnly': True}}))
        self.assertEqual(state.values, {'noise.mode': 'off', 'battery': {'left': 10, 'charging': []}})
        self.assertFalse(state.accepts('wear.detected', False))

    def test_live_failure_still_attempts_initial_settings_and_writes_failed_report(self):
        import io
        class Client:
            def __init__(self):
                self.values = {'noise.mode': 'off'}
                self.sent = []
                self.closed = False
            def wait(self, predicate):
                state = {'apiVersion': 1, 'values': dict(self.values), 'capabilities': CAPS}
                assert predicate(state)
                return state
            def set(self, command, key, value):
                self.sent.append((key, value))
                if value == 'anc': raise TimeoutError('synthetic lost reply')
                self.values[key] = value
                return {'apiVersion': 1, 'values': dict(self.values), 'capabilities': CAPS}
            def close(self): self.closed = True
        client = Client(); output = io.StringIO()
        report = live.run(self.profile, self.directory, client, output, self.root)
        self.assertFalse(report['passed'])
        self.assertEqual(client.sent, [('noise.mode', 'anc'), ('noise.mode', 'off')])
        self.assertTrue(report['restoration'][0]['passed'])
        self.assertTrue(client.closed)
        self.assertEqual(json.loads(output.getvalue()), report)

    def test_unconfirmed_command_does_not_skip_restoration_on_cached_initial_value(self):
        import io
        import queue
        import types
        client = live.Client.__new__(live.Client)
        client.state = {'apiVersion': 1, 'values': {'noise.mode': 'off'}, 'capabilities': CAPS}
        client.serial = 1; client.events = queue.Queue(); client.pending = set()
        client.process = types.SimpleNamespace(stdin=io.StringIO())
        with patch.object(client, 'wait', side_effect=TimeoutError('synthetic lost reply')):
            with self.assertRaises(TimeoutError): client.set('anc command', 'noise.mode', 'anc')
            with self.assertRaises(TimeoutError): client.set('restore command', 'noise.mode', 'off')
        self.assertEqual(client.process.stdin.getvalue(), 'anc command\nrestore command\n')

    def test_external_change_cannot_label_a_command_readback(self):
        self.recorded()
        path = self.directory/'session.json'; spec = json.loads(path.read_text())
        for step in spec['steps']:
            if step.get('case') == 'noise.mode:anc':
                step['case'] = 'external-change'
                break
        path.write_text(json.dumps(spec))
        with self.assertRaisesRegex(ValueError, 'pending command'):
            evidence.verify(self.profile, self.directory, self.root)

    def test_gallery_requires_a_complete_png(self):
        self.assertFalse(contribution.valid_png(b'\x89PNG\r\n\x1a\n'))
        image = next((ROOT/'docs/gallery').glob('*.png')).read_bytes()
        self.assertTrue(contribution.valid_png(image))
        self.assertFalse(contribution.valid_png(image[:-5]))

    def test_generated_registry_and_readme_use_single_package_source(self):
        self.profile['status'] = 'active'; self.save()
        with self.assertRaisesRegex(ValueError, 'stale'):
            contribution.generate(self.root)
        self.assertEqual(contribution.generate(self.root, write=True), ['Model.js', 'README.md'])
        self.assertEqual(contribution.generate(self.root), [])
        source = (self.root/'Model.js').read_text()
        self.assertIn('"profile": "synthetic"', source)
        self.assertNotIn('var DEVICE_PROFILES', source)
        self.assertIn('devices/synthetic/screenshot.png', (self.root/'README.md').read_text())

    def test_real_runner_records_exact_package_through_shared_session(self):
        import types
        from tests.adapter_api_test import Clock, GattGLib
        from omaphones.runner import main
        self.profile['match']['modelId'] = '123abc'
        self.profile['adapter']['transport'] = {'kind': 'ble-gatt', 'writeHandle': '0x0001', 'notifyHandle': '0x0002'}
        self.save()
        glib = GattGLib(Clock()); outputs = []; created = []
        class Loop:
            def __init__(self): self.stopped = False
            def quit(self): self.stopped = True
            def run(self):
                while not self.stopped:
                    for fd, _condition, callback in list(glib.watchers.values()): callback(fd, glib.IO_IN)
        glib.MainLoop = Loop
        class Transport:
            def __init__(self, *_): created.append(self); self.closed = False
            def start(self): self.deliver(Event('connected'))
            def write(self, data): self.record('tx', data)
            def close(self): self.closed = True
        from omaphones.api import Event
        # A queued notification arrives in a subsequent host callback, as it does on a real link.
        chunks = iter([b'{"apiVersion":1,"control":"noise.mode","value":"anc"}\n', b''])
        first = True
        def read(*_):
            nonlocal first
            if first:
                created[0].deliver(Event('received', b'off\n')); first = False
            return next(chunks)
        repository = types.ModuleType('gi.repository')
        repository.GLib = glib; repository.GLibUnix = types.SimpleNamespace(signal_add=lambda *_: 0)
        context = {**devices.identity(INFO), 'modelId': '123abc', 'bleAddress': '11:22:33:44:55:66'}
        digest = devices.implementation_hash(self.profile, self.directory, self.root)
        path = self.directory/'capture.jsonl'
        with patch.dict('sys.modules', {'gi.repository': repository}), patch('omaphones.runner.ROOT', self.root), \
             patch('omaphones.runner.arm_parent_death_signal'), patch('omaphones.runner.emit', side_effect=outputs.append), \
             patch('omaphones.runner.os.read', side_effect=read), patch('omaphones.devices.implementation_hash', return_value=digest), \
             patch.dict('omaphones.transports.TRANSPORTS', {'ble-gatt': Transport}):
            code = main(['--device', 'synthetic', '--allow-draft', '--context', json.dumps(context), '--capture', str(path)])
        self.assertEqual(code, 0)
        self.assertTrue(created[0].closed)
        events = evidence.capture(path, self.profile)
        self.assertEqual([e['data'] for e in events.values() if e['direction'] == 'tx'], [b'query\n'.hex(' '), b'anc\n'.hex(' ')])
        self.assertEqual(outputs[-1]['values'], {'noise.mode': 'off'})
        self.assertEqual(next(iter(events.values()))['data']['implementation'], digest)

    def test_session_scaffolder_keeps_inputs_and_leaves_expectations_unapproved(self):
        import contextlib
        import io
        import runpy
        events = self.recorded()
        (self.directory/'session.json').unlink()
        namespace = runpy.run_path(str(ROOT/'tools/add-device'))
        with contextlib.redirect_stdout(io.StringIO()): namespace['scaffold_session'](self.directory)
        spec = json.loads((self.directory/'session.json').read_text())
        self.assertEqual([s['feed'] for s in spec['steps'] if 'feed' in s], [key for key, e in events.items() if e['direction'] in evidence.INPUTS])
        self.assertTrue(all(s['expect'] is None for s in spec['steps'] if 'expect' in s))
        self.assertEqual(spec['steps'][-1]['sent'], [key for key, e in events.items() if e['direction'] == 'tx'])
        with self.assertRaisesRegex(ValueError, 'independently reviewed'):
            evidence.verify(self.profile, self.directory, self.root)
