"""Regression cases from the three-agent API review; all scenarios are offline."""
from copy import deepcopy
import io
from pathlib import Path
import queue
import types
import unittest
from unittest.mock import patch

from omaphones.api import Protocol
from omaphones.evidence import execute
from omaphones.live import Client
from omaphones.testing import Replay
from tests import adapter_codecs_test as codecs

CAPS = {'noise.mode': {'values': ['off', 'anc']}}


class ImmediateQueue(queue.Queue):
    def get(self, block=True, timeout=None):
        return super().get(block=False)


class ReviewRegressions(unittest.TestCase):
    def test_battery_only_snapshot_cannot_confirm_restore_but_same_value_mode_rx_can(self):
        class Adapter(Protocol):
            def received(self, data):
                if data == b'mode':
                    self.report({'noise.mode': 'off'}, CAPS, observed=('noise.mode',))
                else:
                    self.report({'noise.mode': 'off', 'battery': {'headset': 50}},
                                {**CAPS, 'battery': {'readOnly': True}}, observed=('battery',))
        replay = Replay(Adapter())
        client = Client.__new__(Client)
        client.state = {}; client.serial = 0; client.pending = set(); client.events = ImmediateQueue()
        replay.session.observer = lambda state, fields, changed: client.events.put({**state.snapshot(), 'observed': fields})
        replay.receive(b'mode')
        client.wait(lambda state: state.get('values', {}).get('noise.mode') == 'off')
        client.pending.add('noise.mode')  # Earlier request reached the device; its mode reply was lost.
        response = b'battery'
        class Sink(io.StringIO):
            def flush(self): replay.receive(response)
        client.process = types.SimpleNamespace(stdin=Sink())
        with self.assertRaises(TimeoutError): client.set('restore off', 'noise.mode', 'off')
        self.assertEqual(client.pending, {'noise.mode'})
        response = b'mode'  # Explicit readback, even though the merged value did not change.
        result = client.set('restore off', 'noise.mode', 'off')
        self.assertEqual(result['observed'], ['noise.mode'])
        self.assertEqual(client.pending, set())
        self.assertEqual(client.process.stdin.getvalue(), 'restore off\nrestore off\n')

    def test_cached_fields_in_native_full_snapshots_are_not_fresh_observations(self):
        for brand in ('bose', 'oppo', 'nothing'):
            with self.subTest(brand=brand):
                replay = codecs.new(brand)
                if brand == 'bose': replay.session.adapter.probing = False
                frames = [codecs.wire(brand, step['device']) for step in codecs.pin_for(brand)['steps'] if 'device' in step]
                seen = []
                replay.session.observer = lambda state, fields, changed: seen.append((deepcopy(state.values), fields))
                for frame in frames: replay.receive(frame)
                batteries = [(values, fields) for values, fields in seen if fields == ['battery']]
                self.assertTrue(batteries, brand)
                self.assertTrue(all('noise.mode' in values for values, _ in batteries))
                self.assertIsNone(replay.session.exit_code, replay.session.message)

    def test_sony_wear_reply_does_not_confirm_cached_mode(self):
        from omaphones.registry import get_adapter, load_protocol
        adapter = load_protocol(get_adapter('sony'), {'name': 'WH-1000XM6'})
        adapter.inquired = 0x17; adapter.mode = 'off'
        # Exercise the actual wear reporter through the protocol event path.
        adapter.received = lambda data: adapter.on_wear(True)
        replay = Replay(adapter); seen = []
        replay.session.observer = lambda state, fields, changed: seen.append(fields)
        replay.receive(b'synthetic wear callback')
        self.assertEqual(seen, [['wear.detected']])
        self.assertEqual(replay.session.state.values['noise.mode'], 'off')

    def test_stop_exception_closes_transport_and_ends_loop_once(self):
        class Adapter(Protocol):
            def connected(self): self.schedule(100, 'tick')
            def tick(self): self.write(b'late')
            def on_event(self, event):
                if event.kind == 'stop': raise RuntimeError('stop failed')
                return super().on_event(event)
        replay = Replay(Adapter()); ended = []
        replay.session.ended = lambda *args: ended.append(args)
        replay.event('connected'); replay.event('stop'); replay.session.finish(0); replay.advance(1000)
        self.assertTrue(replay.closed)
        self.assertEqual(replay.clock.pending, {})
        self.assertEqual(replay.sent, [])
        self.assertEqual(len(ended), 1)
        self.assertEqual(ended[0][0], 1)
        self.assertIn('stop failed', ended[0][1])

    def evidence(self, boundary, actual_writes, tx_records, labels=('initial',), two_rx=False):
        class Adapter(Protocol):
            def connected(self):
                for data in actual_writes: self.write(data)
            def received(self, data): self.report({'noise.mode': data.decode()}, CAPS)
        events = {'meta': {'direction': 'metadata', 'data': {'context': {}, 'boundary': boundary}},
                  'open': {'direction': 'event', 'data': {'kind': 'connected'}}}
        for i, data in enumerate(tx_records): events['tx' + str(i)] = {'direction': 'tx', 'data': data.hex()}
        events['rx'] = {'direction': 'rx', 'data': b'off'.hex()}
        steps = [{'feed': 'open'}, {'feed': 'rx'}]
        if two_rx:
            events['rx2'] = {'direction': 'rx', 'data': b'anc'.hex()}
            steps.append({'feed': 'rx2'})
        expected = {'apiVersion': 1, 'values': {'noise.mode': 'anc' if two_rx else 'off'}, 'capabilities': CAPS}
        steps.extend({'expect': expected, 'case': label} for label in labels)
        steps.append({'sent': ['tx' + str(i) for i in range(len(tx_records))]})
        with patch('omaphones.devices.open_protocol', return_value=Adapter()):
            return execute({'id': 'fixture', 'capabilities': CAPS}, Path('/tmp/unused'), events, {'apiVersion': 1, 'steps': steps})

    def test_gatt_requires_payload_boundaries_in_both_directions(self):
        for actual, recorded in (([b'a', b'b'], [b'ab']), ([b'ab'], [b'a', b'b'])):
            with self.subTest(actual=actual), self.assertRaisesRegex(AssertionError, 'GATT payload boundaries'):
                self.evidence('gatt-notification/client-command-payload', actual, recorded)
        self.assertEqual(self.evidence('gatt-notification/client-command-payload', [b'a', b'b'], [b'a', b'b']), {'initial'})

    def test_stream_capture_can_preserve_partial_write_chunks(self):
        self.assertEqual(self.evidence('stream', [b'ab'], [b'a', b'b']), {'initial'})
        self.assertEqual(self.evidence('stream', [b'a', b'b'], [b'ab']), {'initial'})

    def test_first_reply_cannot_double_as_external_change(self):
        with self.assertRaisesRegex(ValueError, 'previously observed control'):
            self.evidence('stream', [b'a'], [b'a'], labels=('initial', 'external-change'))
        self.assertEqual(self.evidence('stream', [b'a'], [b'a'], labels=('external-change',), two_rx=True), {'external-change'})

    def test_observation_markers_respect_limits_and_rx_only(self):
        class Adapter(Protocol):
            def received(self, data):
                self.report({'noise.mode': 'off', 'battery': {'headset': 50}},
                            {**CAPS, 'battery': {'readOnly': True}}, observed=('battery',))
                self.schedule(1, 'tick')
            def tick(self): self.report({'noise.mode': 'anc'}, CAPS)
        replay = Replay(Adapter(), limits=CAPS); seen = []
        replay.session.observer = lambda state, fields, changed: seen.append(fields)
        replay.receive(b'synthetic status'); replay.advance(1)
        self.assertEqual(seen, [[], []])
        self.assertNotIn('battery', replay.session.state.values)
