"""Author workflow, registry conflicts and new API pins; all data here is synthetic."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from omaphones.registry import ROOT, descriptors, get_adapter, load_protocol, model_parameters
from omaphones.scaffold import new_adapter, new_model
from omaphones.checking import check_boundary, suite_for
from omaphones.testing import Replay
from omaphones.api import Protocol, Event
from omaphones import cache
from omaphones.runner import main, parse_command


class Tools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'adapters').mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            new_adapter(['example', '--uuid', '10000000-0000-0000-0000-000000000001', '--priority', '70'], self.root)
        self.package = self.root / 'adapters/example'

    def save(self, relative, value):
        (self.package / relative).write_text(json.dumps(value))

    def test_author_tools_refuse_the_live_plugin_directory(self):
        with patch('omaphones.scaffold.installed_root', return_value=self.root):
            with self.assertRaisesRegex(ValueError, 'isolated clone'):
                new_adapter(['another', '--uuid', '10000000-0000-0000-0000-000000000002', '--priority', '80'], self.root)
        self.assertFalse((self.root/'adapters/another').exists())

    def test_generated_draft_is_inert_and_overwrite_is_refused(self):
        self.assertEqual(descriptors(self.root), [])
        self.assertEqual(len(descriptors(self.root, drafts=True)), 1)
        with self.assertRaises(FileExistsError), contextlib.redirect_stdout(io.StringIO()):
            new_adapter(['example', '--uuid', '10000000-0000-0000-0000-000000000001', '--priority', '70'], self.root)
        result = unittest.TestResult()
        suite_for('example', self.root, include_drafts=True).run(result)
        self.assertEqual(len(result.failures), 1)

    def test_draft_model_does_not_change_effective_parameters(self):
        with contextlib.redirect_stdout(io.StringIO()):
            new_model(['example', 'headset', '--name', 'Headset', '--owner', 'test-owner'], self.root)
        row = descriptors(self.root, drafts=True)[0]
        self.assertEqual(model_parameters(row, {'name': 'Headset'}, self.root), {})
        model = json.loads((self.package / 'models/headset.json').read_text())
        model['status'] = 'active'
        self.save('models/headset.json', model)
        row['status'] = 'active'; self.save('adapter.json', row)
        with self.assertRaisesRegex(ValueError, 'owner, pin and capture'):
            descriptors(self.root)

    def test_overlapping_uuid_claims_and_wrong_api_are_rejected(self):
        row = descriptors(self.root, drafts=True)[0]
        row['status'] = 'active'; self.save('adapter.json', row)
        peer = self.root / 'adapters/peer'; peer.mkdir()
        (peer/'protocol.py').write_text((self.package/'protocol.py').read_text())
        (peer/'adapter.json').write_text(json.dumps({**row, 'id': 'peer', 'priority': 80}))
        with self.assertRaisesRegex(ValueError, 'overlapping'):
            descriptors(self.root)
        row['apiVersion'] = 2; self.save('adapter.json', row)
        with self.assertRaisesRegex(ValueError, 'API version'):
            descriptors(self.root)

    def test_boundary_refuses_platform_imports_and_file_access(self):
        for source in ('import socket', 'from os import read', 'open("state.json")', '__import__("os")'):
            (self.package/'protocol.py').write_text(source)
            with self.assertRaises(ValueError):
                check_boundary(self.package/'protocol.py')

    def test_new_package_can_run_its_pin_without_changing_host_code(self):
        # Synthetic protocol for testing the SDK, never hardware evidence.
        (self.package/'protocol.py').write_text('''from omaphones.api import Protocol
class Adapter(Protocol):
    def connected(self):
        self.write(b"query")
    def received(self, data):
        if data == b"off":
            self.report({"noise.mode": "off"}, {"noise.mode": {"values": ["off", "anc"]}})
    def command(self, control, value):
        self.write(value.encode())
''')
        (self.package/'tests/protocol_test.py').write_text('import unittest\n')
        (self.package/'captures/synthetic.txt').write_text('synthetic test input, not a device recording')
        prefix = 'adapters/example/'
        self.save('pins/headset.json', {'apiVersion': 1, 'adapter': 'example', 'model': 'Headset', 'owner': 'test-owner', 'capture': prefix+'captures/synthetic.txt', 'context': {'name': 'Headset'}, 'steps': [
            {'event': 'connected'}, {'sent': ['71 75 65 72 79']}, {'device': '6f 66 66'}, {'values': {'noise.mode': 'off'}},
            {'command': {'control': 'noise.mode', 'value': 'anc'}}, {'sent': ['71 75 65 72 79', '61 6e 63']}, {'values': {'noise.mode': 'off'}}]})
        self.save('models/headset.json', {'id': 'headset', 'match': {'name': 'Headset'}, 'parameters': {}, 'owners': ['test-owner'], 'captures': [prefix+'captures/synthetic.txt'], 'pins': [prefix+'pins/headset.json']})
        row = json.loads((self.package/'adapter.json').read_text()); row['status'] = 'active'; self.save('adapter.json', row)
        result = unittest.TestResult(); suite_for('example', self.root).run(result)
        self.assertEqual(result.errors, []); self.assertEqual(result.failures, [])
        self.assertEqual(result.testsRun, 1)

    def test_command_validation_and_bad_identity_do_not_touch_bluetooth(self):
        for line in ('[]', '{}', '{"apiVersion": 2,"control":"noise.mode","value":"anc"}', 'set unknown', 'voice maybe'):
            self.assertIsNone(parse_command(line))
        with patch('omaphones.runner.arm_parent_death_signal'), patch('omaphones.runner.emit') as emit:
            code = main(['sony', '--context', '{"address":"bad"}'])
        self.assertEqual(code, 4)
        self.assertFalse(emit.call_args.args[0]['modes'])

    def test_cache_normalizes_old_entries_and_never_retires_an_answered_model(self):
        with patch.dict('os.environ', {'XDG_STATE_HOME': str(self.root)}):
            for _ in range(3):
                cache.remember('model', False)
            path = self.root/'omaphones/mode-support.json'
            self.assertFalse(json.loads(path.read_text())['model']['supported'])
            cache.remember('model', True)
            cache.remember('model', False)
            self.assertEqual(json.loads(path.read_text())['model'], {'supported': True})

    def test_cancelled_timer_and_stop_cannot_write(self):
        class Timed(Protocol):
            def connected(self):
                token = self.schedule(1, 'tick')
                self.cancel_timer(token)
                self.schedule(2, 'tick')
            def tick(self):
                self.write(b'bad')
        replay = Replay(Timed()); replay.event('connected'); replay.advance(1)
        self.assertEqual(replay.sent, [])
        replay.event('stop'); replay.advance(10)
        self.assertEqual(replay.sent, [])
        self.assertEqual(replay.clock.pending, {})
