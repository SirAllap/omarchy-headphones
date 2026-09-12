"""Shared runtime validation and cache regressions."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from omaphones import cache
from omaphones.api import Protocol
from omaphones.testing import Replay
from omaphones.runner import main, parse_command


class Tools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

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
