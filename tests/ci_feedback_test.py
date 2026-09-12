"""Deliberately broken contributions must produce useful CI failures."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from omaphones import contracts, contribution, feedback
from omaphones.registry import ROOT
from tests import device_packages_test as fixtures


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.Packages()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def record(self, restore=False):
        self.f.recorded()
        path = self.f.directory/'capture.jsonl'
        events = [json.loads(line) for line in path.read_text().splitlines()]
        if restore:
            index = max(i for i, e in enumerate(events) if e['direction'] == 'rx')
            events.insert(index, {'id': 'restore', 'direction': 'command', 'encoding': 'json', 'data': {'control': 'noise.mode', 'value': 'off'}})
            for i, e in enumerate(events): e['timeNs'] = i
            path.write_text('\n'.join(json.dumps(e) for e in events))
        return events

    def test_initial_state_cannot_stand_in_for_an_answered_command(self):
        self.record()
        with self.assertRaisesRegex(ValueError, 'noise.mode:off.*tools/add-device live synthetic'):
            contracts.roundtrips(self.f.profile, self.f.directory, self.f.root)

    def test_confirmed_commands_coalescing_and_interleaved_sessions_pass(self):
        self.record(restore=True)
        self.assertIn('2 declared control values', contracts.roundtrips(self.f.profile, self.f.directory, self.f.root))
        self.assertIn('RX batches merged', contracts.coalesced(self.f.profile, self.f.directory, self.f.root))
        self.assertIn('two interleaved', contracts.isolation(self.f.profile, self.f.directory, self.f.root))

    def test_coalescing_catches_parser_that_only_consumes_one_frame(self):
        self.record()
        path = self.f.directory/'protocol.py'
        path.write_text(path.read_text().replace('while b"\\n" in self.buffer:', 'if b"\\n" in self.buffer:'))
        with self.assertRaisesRegex(ValueError, 'merging adjacent RX.*received'):
            contracts.coalesced(self.f.profile, self.f.directory, self.f.root)

    def test_interleaving_catches_class_state_leaking_between_sessions(self):
        self.record()
        path = self.f.directory/'protocol.py'
        source = path.read_text().replace('class Adapter(Protocol):', 'class Adapter(Protocol):\n    calls = 0')
        source = source.replace('        self.buffer += data', '        type(self).calls += 1\n        self.write(str(type(self).calls).encode())\n        self.buffer += data')
        path.write_text(source)
        with self.assertRaisesRegex(ValueError, 'sessions diverged at capture event.*__init__'):
            contracts.isolation(self.f.profile, self.f.directory, self.f.root)

    def test_malformed_manifest_and_boundary_error_have_precise_files_and_lines(self):
        path = self.f.directory/'protocol.py'; path.write_text('# comment\nimport socket\n')
        report = contribution.readiness(self.f.directory, self.f.root)
        failure = next(c for c in report['checks'] if c['check'] == 'adapter')
        self.assertEqual((failure['file'], failure['line']), ('devices/synthetic/protocol.py', 2))
        self.assertIn('tools/add-device check synthetic', failure['reproduce'])
        (self.f.directory/'device.json').write_text('{\n "broken":\n}')
        report = contribution.readiness(self.f.directory, self.f.root)
        self.assertFalse(report['passed'])
        self.assertEqual(report['checks'][0]['line'], 3)
        self.assertIn('devices/synthetic/device.json', feedback.annotations([report]))

    def test_capture_json_error_points_to_capture_line(self):
        self.record()
        path = self.f.directory/'capture.jsonl'; path.write_text(path.read_text() + '\n{bad}')
        report = contribution.readiness(self.f.directory, self.f.root)
        failure = next(c for c in report['checks'] if c['check'] == 'capture and replay')
        self.assertEqual(failure['file'], 'devices/synthetic/capture.jsonl')
        self.assertGreater(failure['line'], 1)

    def test_annotations_and_summary_escape_author_controlled_text(self):
        reports = [{'device': 'test', 'checks': [{'check': 'bad, title', 'passed': False, 'file': 'a,b.py', 'line': 2,
            'detail': 'one\n::warning::injected 100% <script>|', 'fix': 'fix\rthis', 'reproduce': 'tools/check'}]}]
        output = feedback.annotations(reports)
        self.assertEqual(len(output.splitlines()), 1)
        self.assertIn('a%2Cb.py', output)
        self.assertIn('%0A::warning::', output)
        self.assertIn('100%25', output)
        summary = contribution.markdown(reports)
        self.assertNotIn('<script>', summary)
        self.assertIn('&#124;', summary)
        self.assertIn('Fix:', summary)

    def test_raw_owner_capture_changes_name_the_file(self):
        root = self.f.root
        subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
        (root/'docs/captures').mkdir(parents=True)
        path = root/'docs/captures/owner.txt'; path.write_text('observed bytes')
        subprocess.run(['git', 'add', 'docs/captures/owner.txt'], cwd=root, check=True)
        subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgsign=false', 'commit', '-qm', 'baseline'], cwd=root, check=True)
        path.write_text('replacement')
        with self.assertRaisesRegex(ValueError, 'docs/captures/owner.txt') as caught:
            contribution.immutable(root, 'HEAD')
        self.assertEqual(caught.exception.filename, 'docs/captures/owner.txt')

    def test_cli_reports_multiple_broken_packages_instead_of_crashing_on_first(self):
        import contextlib
        import io
        import runpy
        import sys
        other = self.f.root/'devices/other'; other.mkdir()
        (other/'device.json').write_text('{broken')
        (self.f.directory/'device.json').write_text('{broken')
        output = io.StringIO()
        module = runpy.run_path(str(ROOT/'tools/add-device'))
        with patch.object(module['profiles'], 'ROOT', self.f.root), patch('sys.argv', ['add-device', 'check', '--github', '--json']), \
             patch('omaphones.contribution.generate', return_value=[]), patch('omaphones.contribution.immutable', return_value='unchanged'), contextlib.redirect_stdout(output):
            code = module['main']()
        self.assertEqual(code, 1)
        self.assertIn('devices/other/device.json', output.getvalue())
        self.assertIn('devices/synthetic/device.json', output.getvalue())
        self.assertEqual(output.getvalue().count('::error '), 2)

    def test_command_without_any_wire_write_does_not_count_as_confirmation(self):
        self.record(restore=True)
        path = self.f.directory/'protocol.py'
        path.write_text(path.read_text().replace('        self.write(value.encode() + b"\\n")', '        pass'))
        with self.assertRaisesRegex(ValueError, 'wire write.*noise.mode:anc'):
            contracts.roundtrips(self.f.profile, self.f.directory, self.f.root)
