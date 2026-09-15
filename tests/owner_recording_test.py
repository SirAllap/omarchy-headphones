"""Synthetic BTSnoop containers and action logs, never hardware evidence."""
import csv
import json
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import patch

from omaphones import owner_recording as recording
from omaphones.registry import ROOT

INFO = 'Device AA:BB:CC:DD:EE:FF\n\tName: Synthetic Headset\n\tUUID: Test (10000000-0000-0000-0000-000000000001)\n'


def btsnoop(records, link=2001):
    data = b'btsnoop\0' + struct.pack('>II', 1, link)
    for flags, payload, timestamp in records:
        data += struct.pack('>IIIIq', len(payload), len(payload), flags, 0, recording.EPOCH_US + timestamp) + payload
    return data


def fixture(directory, events=None):
    """Explicit synthetic wire bytes for archive/provenance tests only."""
    with patch('omaphones.owner_recording.revision', return_value={'commit': 'synthetic', 'codeSha256': 'test', 'dirty': False}), \
         patch('omaphones.owner_recording.subprocess.check_output', return_value='synthetic-btmon'):
        recording.initialize(directory, owner='test-owner', info=INFO, firmware='synthetic',
                             scenario='synthetic fixture', controller='hci0', root=ROOT)
    start = recording.read_json(directory / 'session.json')['startedUnixNs'] // 1000
    events = events or [('tx', b'header-query'), ('rx', b'header-off')]
    (directory / 'traffic.btsnoop').write_bytes(btsnoop([
        (4 if direction == 'tx' else 5, data, start + index * 1000)
        for index, (direction, data) in enumerate(events)]))
    recording.mark(directory, 'action', 'owner', 'Synthetic button press')
    recording.mark(directory, 'observation', 'owner', 'Synthetic observation, no physical device')
    recording.seal(directory, 'Synthetic test fixture only')


class OwnerRecordingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name) / 'session'
        fixture(self.directory)

    def test_format_timestamps_directions_unknown_monitor_record_preserved(self):
        path = self.directory / 'test.btsnoop'
        path.write_bytes(btsnoop([(2 << 16 | 4, b'\x01\x02', 123),
                                 (2 << 16 | 5, b'\xff', 124), (2 << 16 | 12, b'opaque', 125)]))
        packets = list(recording.packets(path))
        self.assertEqual([p['direction'] for p in packets], ['tx', 'rx', None])
        self.assertEqual([p['unixUs'] for p in packets], [123, 124, 125])
        self.assertEqual(packets[2]['data'], b'opaque')
        self.assertEqual(packets[0]['controller'], 2)
        self.assertEqual(packets[1]['fileOffset'], 16 + 24 + 2)

    def test_h4_incoming_packet_keeps_type_byte(self):
        path = self.directory / 'h4.btsnoop'
        path.write_bytes(btsnoop([(1, b'\x02data', 123)], 1002))
        packet = next(recording.packets(path))
        self.assertEqual((packet['direction'], packet['data']), ('rx', b'\x02data'))

    def test_loss_truncation_bad_headers_and_clock_reversal_rejected(self):
        valid = btsnoop([(4, b'ab', 100), (5, b'cd', 101)])
        variants = [b'', valid[:12], valid[:20], valid[:-1],
                    valid[:16] + struct.pack('>IIIIq', 10, 2, 4, 0, 100) + b'ab',
                    valid[:16] + struct.pack('>IIIIq', 2, 2, 4, 1, 100) + b'ab',
                    btsnoop([(4, b'a', 101), (5, b'b', 100)]),
                    valid[:12] + struct.pack('>I', 9999) + valid[16:]]
        for data in variants:
            with self.subTest(data=data[:24]):
                path = self.directory / 'bad.btsnoop'; path.write_bytes(data)
                with self.assertRaises(ValueError): list(recording.packets(path))

    def test_archive_integrity_and_immutable_timeline(self):
        self.assertEqual(recording.verify(self.directory)['packets'], 2)
        with self.assertRaisesRegex(ValueError, 'sealed'):
            recording.mark(self.directory, 'note', 'owner', 'late edit')
        with self.assertRaises(FileExistsError): recording.seal(self.directory, 'overwrite')
        with (self.directory / 'traffic.btsnoop').open('ab') as output: output.write(b'late')
        with self.assertRaisesRegex(ValueError, 'changed'): recording.verify(self.directory)

    def test_packet_slices_have_independent_bytes_direction_and_bounds(self):
        self.assertEqual(recording.extract(self.directory, [[2, 7, 3]], 'rx'), b'off')
        for slices, direction in [([[2, 7, 3]], 'tx'), ([[9, 0, 1]], 'rx'),
                                  ([[2, 7, 4]], 'rx'), ([[2, 7, 2], [2, 8, 2]], 'rx'),
                                  ([[2, -1, 1]], 'rx'), ([], 'rx')]:
            with self.subTest(slices=slices, direction=direction):
                with self.assertRaises(ValueError): recording.extract(self.directory, slices, direction)

    def test_csv_multiline_quoted_owner_observation_roundtrips(self):
        (self.directory / 'manifest.json').unlink()
        description = 'Pressed "ANC", then waited\nheard a change, uncertain about strength'
        recording.mark(self.directory, 'observation', 'owner', description)
        with (self.directory / 'actions.csv').open(newline='') as source:
            self.assertEqual(list(csv.DictReader(source))[-1]['description'], description)
        recording.seal(self.directory, 'synthetic')
        self.assertEqual(recording.verify(self.directory)['timelineEntries'], 3)

    def test_no_owner_observation_or_clock_mismatch_cannot_seal(self):
        (self.directory / 'manifest.json').unlink()
        path = self.directory / 'actions.csv'
        with path.open(newline='') as source: rows = list(csv.DictReader(source))
        for changed in [rows[:1], [{**r, 'unix_ns': int(r['unix_ns']) + 5_000_000_000} for r in rows]]:
            with path.open('w', newline='') as output:
                writer = csv.DictWriter(output, fieldnames=recording.FIELDS)
                writer.writeheader(); writer.writerows(changed)
            with self.assertRaises(ValueError): recording.seal(self.directory, 'synthetic')
            self.assertFalse((self.directory / 'manifest.json').exists())

    def test_capture_change_during_seal_leaves_no_manifest(self):
        (self.directory / 'manifest.json').unlink()
        original = recording.inspect
        def changed(directory):
            result = original(directory)
            with (directory / 'traffic.btsnoop').open('ab') as output: output.write(b'late')
            return result
        with patch('omaphones.owner_recording.inspect', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'changed'): recording.seal(self.directory, 'synthetic')
        self.assertFalse((self.directory / 'manifest.json').exists())

    def test_independent_source_verifies_every_replay_byte(self):
        from omaphones import evidence
        package = self.directory.parent / 'package'; package.mkdir()
        source = package / 'source-session'
        fixture(source, [('tx', b'header-query'), ('rx', b'header-off')])
        meta = recording.read_json(source / 'session.json')
        profile = {'id': 'synthetic', 'owner': 'test-owner', 'match': {'names': ['Synthetic Headset'], 'uuids': meta['device']['uuids']}}
        events = [{'id': '0', 'timeNs': 0, 'direction': 'metadata', 'encoding': 'json',
                   'data': {'apiVersion': 1, 'device': 'synthetic', 'owner': 'test-owner', 'context': meta['device'], 'boundary': 'stream'}}]
        for number, direction, data in [(1, 'tx', b'query'), (2, 'rx', b'off')]:
            events.append({'id': str(number), 'timeNs': number, 'direction': direction, 'encoding': 'hex', 'data': data.hex(),
                           'source': {'sha256': recording.digest(source / 'traffic.btsnoop'), 'slices': [[number, 7, len(data)]]}})
        def save(): (package / 'capture.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
        save(); self.assertIn('provenance', evidence.independent_source(profile, package))
        events.append({**events[-1], 'id': '3', 'timeNs': 3})
        save()
        with self.assertRaisesRegex(ValueError, 'reused'): evidence.independent_source(profile, package)
        events.pop()
        events[-1]['data'] = b'anc'.hex(); save()
        with self.assertRaisesRegex(ValueError, 'differs'): evidence.independent_source(profile, package)
        events[-1].pop('source'); save()
        with self.assertRaisesRegex(ValueError, 'source hash'): evidence.independent_source(profile, package)
