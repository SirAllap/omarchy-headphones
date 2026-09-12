"""Drive the actual command-line runner with fake GLib and transport endpoints."""
import types
import unittest
from unittest.mock import patch

from omaphones.api import Event, Protocol
from omaphones.runner import main
from tests.adapter_api_test import Clock, GattGLib


class RunnerTests(unittest.TestCase):
    def run_host(self, missing=False):
        frames = []; outputs = []; transports = []
        clock = Clock(); glib = GattGLib(clock)
        class Loop:
            def __init__(self):
                self.stopped = False
            def quit(self):
                self.stopped = True
            def run(self):
                while not self.stopped:
                    callbacks = list(glib.watchers.values())
                    if not callbacks:
                        raise AssertionError('runner has no stdin watcher')
                    for fd, _, callback in callbacks:
                        callback(fd, glib.IO_IN)
        glib.MainLoop = Loop
        class Adapter(Protocol):
            def received(self, data):
                self.report({'noise.mode': data.decode()}, {'noise.mode': {'values': ['off', 'anc']}})
            def command(self, control, value):
                self.write(value.encode())
        class Transport:
            def __init__(self, *args):
                self.closed = False
                transports.append(self)
            def start(self):
                if missing:
                    raise FileNotFoundError('missing helper')
                self.deliver(Event('connected'))
                self.deliver(Event('received', b'off'))
            def write(self, data):
                frames.append(data)
            def close(self):
                self.closed = True
        repository = types.ModuleType('gi.repository')
        repository.GLib = glib
        repository.GLibUnix = types.SimpleNamespace(signal_add=lambda *args: 0)
        row = {'entry': 'protocol.py', 'transport': {'kind': 'ble-gatt', 'addressField': 'bleAddress'}}
        chunks = [b'{"apiVersion":1,"control":"noise.', b'mode","value":"anc"}\n', b'']
        with patch.dict('sys.modules', {'gi.repository': repository}), \
             patch('omaphones.runner.arm_parent_death_signal'), \
             patch('omaphones.runner.get_adapter', return_value=row), \
             patch('omaphones.runner.load_protocol', return_value=Adapter()), \
             patch('omaphones.runner.emit', side_effect=outputs.append), \
             patch('omaphones.runner.os.read', side_effect=chunks), \
             patch.dict('omaphones.transports.TRANSPORTS', {'ble-gatt': Transport}):
            code = main(['example', '--context', '{"bleAddress":"AA:BB:CC:DD:EE:FF"}'])
        return code, frames, outputs, transports

    def test_partial_json_command_observed_state_and_clean_eof(self):
        code, frames, outputs, transports = self.run_host()
        self.assertEqual(code, 0)
        self.assertEqual(frames, [b'anc'])
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0]['mode'], 'off')
        self.assertEqual(outputs[0]['values'], {'noise.mode': 'off'})
        self.assertEqual(outputs[0]['apiVersion'], 1)
        self.assertTrue(transports[0].closed)

    def test_missing_helper_is_setup_failure_and_closes_transport(self):
        code, frames, outputs, transports = self.run_host(missing=True)
        self.assertEqual(code, 4)
        self.assertEqual(frames, [])
        self.assertEqual(outputs, [{'modes': False, 'error': 'missing helper'}])
        self.assertTrue(transports[0].closed)
