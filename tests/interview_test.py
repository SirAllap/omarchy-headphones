"""Owner flow checks using synthetic devices; never hardware evidence."""
import copy
import csv
import io
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from omaphones import interview, live, owner_recording
from tests.owner_recording_test import fixture


class Scripted:
    channel = 'terminal'
    def __init__(self, answers, on_question=None):
        self.answers = iter(answers)
        self.questions = []
        self.events = []
        self.on_question = on_question
    def notify(self, event): self.events.append(event)
    def ask(self, question, timeout):
        self.questions.append(question)
        if self.on_question: self.on_question(question)
        choice = next(self.answers)
        if isinstance(choice, BaseException): raise choice
        if isinstance(choice, tuple): choice, text = choice
        else: text = ''
        return interview.make_answer(question, choice, text, self.channel)
    def close(self): pass


CAPS = {'noise.mode': {'values': ['off', 'anc']}}
PROFILE = {'id': 'synthetic', 'owner': 'test-owner', 'capabilities': CAPS}


class FakeDevice:
    def __init__(self, error=None):
        self.state = {'values': {'noise.mode': 'off'}, 'capabilities': CAPS}
        self.commands = []
        self.closed = False
        self.error = error
        self.serial = 0
    def wait(self, predicate, **kwargs):
        if not predicate(self.state): raise TimeoutError('synthetic missing state')
        return copy.deepcopy(self.state)
    def set(self, command, key, value):
        self.commands.append((key, value))
        if self.error and value == 'anc': raise self.error
        self.state['values'][key] = value
        return copy.deepcopy(self.state)
    def close(self): self.closed = True


class InterviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name) / 'source'
        fixture(self.directory)
        (self.directory / 'manifest.json').unlink()

    def session(self, answers, callback=None):
        ui = Scripted(answers, callback)
        return interview.Interview(self.directory, ui, timeout=.1), ui

    def test_no_control_before_ready_or_next_control_before_observation(self):
        client = FakeDevice()
        def check(question):
            if question['phase'] == 'readiness': self.assertEqual(client.commands, [])
            if question['phase'] in ('observation', 'decision'):
                self.assertEqual(client.commands, [('noise.mode', 'anc')])
        flow, ui = self.session(['ready', 'quieter', 'continue'], check)
        result = flow.control('anc', 'noise.mode', 'anc', client, 'set anc', PROFILE)
        self.assertEqual(result['values']['noise.mode'], 'anc')
        self.assertEqual(len(flow.observations), 1)
        with (self.directory / 'actions.csv').open(newline='') as source:
            rows = list(csv.DictReader(source))
        records = [(r, json.loads(r['description'])) for r in rows if r['description'].startswith('{')]
        readiness = next(r for r, e in records if e.get('type') == 'owner-answer' and e['phase'] == 'readiness')
        self.assertEqual(readiness['kind'], 'note')
        observed = next(e for r, e in records if r['kind'] == 'observation')
        self.assertEqual(observed['answer'], 'quieter')
        owner_recording.seal(self.directory, 'Synthetic structured UI observation')
        owner_recording.verify(self.directory)
        with self.assertRaisesRegex(ValueError, 'sealed'):
            flow.event('late', 'late event')

    def test_repeat_preserves_uncertainty_and_reestablishes_baseline(self):
        client = FakeDevice()
        flow, ui = self.session(['ready', ('unsure', 'I was distracted'), 'repeat',
                                'ready', 'ready', 'quieter', 'continue'])
        flow.control('anc', 'noise.mode', 'anc', client, 'set anc', PROFILE)
        self.assertEqual(client.commands, [('noise.mode', 'anc'), ('noise.mode', 'off'), ('noise.mode', 'anc')])
        self.assertEqual([a['attempt'] for a in flow.observations], [1, 2])
        self.assertEqual(flow.observations[0]['text'], 'I was distracted')
        self.assertEqual(len({q['requestId'] for q in ui.questions}), len(ui.questions))

    def test_pause_and_skip_send_nothing(self):
        client = FakeDevice()
        flow, _ = self.session(['pause', 'resume', 'skip'])
        self.assertIsNone(flow.control('anc', 'noise.mode', 'anc', client, '', PROFILE))
        self.assertEqual(client.commands, [])
        self.assertEqual(flow.skipped[0]['stepId'], 'anc')

    def test_stopped_timeout_and_control_failure_restore_and_close(self):
        for reason in ['stop', interview.Stopped('timeout'), KeyboardInterrupt('interrupt')]:
            with self.subTest(reason=reason):
                client = FakeDevice()
                flow, ui = self.session(['ready', 'quieter', reason])
                report = live.run(PROFILE, self.directory, client, io.StringIO(), implementation='synthetic', interview=flow)
                self.assertFalse(report['passed'])
                self.assertTrue(client.closed)
                self.assertEqual(client.state['values']['noise.mode'], 'off')
                self.assertEqual(client.commands[-1], ('noise.mode', 'off'))
                self.assertIn('restoration', [e['phase'] for e in ui.events])
        client = FakeDevice(TimeoutError('failed write'))
        flow, ui = self.session(['ready'])
        report = live.run(PROFILE, self.directory, client, io.StringIO(), implementation='synthetic', interview=flow)
        self.assertFalse(report['passed'])
        self.assertTrue(client.closed)
        self.assertEqual(len(flow.observations), 0)
        self.assertEqual(client.commands[-1], ('noise.mode', 'off'))

    def test_reporting_failure_does_not_suppress_restoration(self):
        client = FakeDevice()
        flow, ui = self.session(['ready', 'quieter', 'stop'])
        original = flow.event
        def failing(phase, *args, **kwargs):
            if phase == 'restoration': raise OSError('disk full')
            return original(phase, *args, **kwargs)
        flow.event = failing
        result = live.run(PROFILE, self.directory, client, io.StringIO(), implementation='synthetic', interview=flow)
        self.assertEqual(result['interviewError'], 'disk full')
        self.assertEqual(client.state['values']['noise.mode'], 'off')
        self.assertTrue(client.closed)

    def test_response_validation_and_required_physical_description(self):
        flow, ui = self.session([])
        q = dict(sessionId='s', requestId='r', phase='completion', choices=['done', 'stop'])
        answer = interview.make_answer(q, 'done', '', 'terminal')
        with self.assertRaisesRegex(ValueError, 'describe'): interview.validate_answer(answer, q, 'terminal')
        answer['text'] = 'Pressed the headset mode button once.'
        interview.validate_answer(answer, q, 'terminal')
        for change in [{'requestId': 'stale'}, {'channel': 'browser'}, {'answer': 'made-up'}, {'version': True}, {'unixNs': 0}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                interview.validate_answer({**answer, **change}, q, 'terminal')

    def test_console_json_and_terminal_save_equivalent_answers(self):
        for mode, line, channel in [('terminal', 'unsure | distracted\n', 'terminal'),
                                    ('json', None, 'assistant-relay')]:
            q = dict(type='owner-question', version=1, sessionId='s', requestId='r',
                     phase='observation', device='synthetic', question='What changed?', choices=['unsure'])
            expected = interview.make_answer(q, 'unsure', 'distracted', channel)
            data = line or json.dumps(expected) + '\n'
            read, write = os.pipe()
            os.write(write, data.encode()); os.close(write)
            with os.fdopen(read) as source:
                ui = interview.Console(mode, source, io.StringIO())
                self.assertEqual(ui.ask(q, .1), expected)
        read, write = os.pipe()
        with os.fdopen(read) as source:
            ui = interview.Console('json', source, io.StringIO())
            with self.assertRaises(interview.Stopped): ui.ask(q, .01)
        os.close(write)

    def test_manual_action_is_described_and_unknown_observation_retained(self):
        flow, _ = self.session(['ready', ('done', 'Pressed the physical button once.'), ('unsure', 'Could not tell.')])
        self.assertTrue(flow.manual('external-change', 'Press the mode button.'))
        self.assertEqual(flow.observations[0]['answer'], 'unsure')
        with (self.directory / 'actions.csv').open(newline='') as source:
            records = list(csv.DictReader(source))
        physical = [json.loads(r['description']) for r in records if r['kind'] == 'action' and r['description'].startswith('{')]
        self.assertEqual(physical[-1]['text'], 'Pressed the physical button once.')

    def test_skipped_controls_cannot_make_a_complete_run(self):
        flow, _ = self.session(['skip', 'skip', 'skip'])
        client = FakeDevice()
        result = live.run(PROFILE, self.directory, client, io.StringIO(), implementation='synthetic', interview=flow)
        self.assertFalse(result['passed'])
        self.assertIn('incompleteReason', result)
        self.assertTrue(client.closed)

    def test_changed_baseline_is_presented_again_before_control(self):
        client = FakeDevice()
        responses = iter([copy.deepcopy(client.state),
                          {'values': {'noise.mode': 'anc'}, 'capabilities': CAPS},
                          {'values': {'noise.mode': 'anc'}, 'capabilities': CAPS}])
        def refresh():
            client.state = next(responses)
            return copy.deepcopy(client.state)
        client.refresh = refresh
        flow, ui = self.session(['ready', 'ready'])
        flow.control('anc', 'noise.mode', 'anc', client, '', PROFILE)
        self.assertEqual(client.commands, [])
        self.assertEqual(len(ui.questions), 2)
        self.assertEqual(ui.questions[1]['context']['baseline']['noise.mode'], 'anc')
        self.assertEqual(flow.observations, [])

    def test_restoration_still_writes_if_timeline_disk_fails(self):
        from omaphones.migration import TimelineClient
        raw = FakeDevice()
        raw.state['values']['noise.mode'] = 'anc'
        client = TimelineClient(raw, self.directory)
        client.begin_restoration()
        with patch('omaphones.migration.recording.mark', side_effect=OSError('full disk')):
            client.set('set off', 'noise.mode', 'off')
        self.assertEqual(raw.state['values']['noise.mode'], 'off')
        self.assertEqual(len(client.log_errors), 2)

    def test_json_demo_completes_over_real_pipes_without_radio(self):
        command = [sys.executable, str(Path(__file__).parents[1] / 'tools/owner-interview-demo'),
                   '--interview', 'json', '--owner-timeout', '2']
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            questions = []
            result = None
            for line in process.stdout:
                event = json.loads(line)
                if event['type'] == 'owner-question':
                    questions.append(event)
                    choice = {'readiness': 'ready', 'observation': 'unsure', 'decision': 'continue'}[event['phase']]
                    answer = interview.make_answer(event, choice, 'Synthetic relay check', 'assistant-relay')
                    process.stdin.write(json.dumps(answer) + '\n'); process.stdin.flush()
                elif event['type'] == 'test-result':
                    result = event['report']
            self.assertEqual(process.wait(timeout=5), 0)
            self.assertTrue(result['synthetic'])
            self.assertTrue(result['completed'])
            self.assertEqual(result['finalState']['values']['noise.mode'], 'off')
            self.assertEqual(result['ownerObservations'][0]['channel'], 'assistant-relay')
            self.assertEqual([q['phase'] for q in questions], ['readiness', 'observation', 'decision'])
        finally:
            if process.poll() is None: process.kill(); process.wait(timeout=5)
            process.stdin.close(); process.stdout.close(); process.stderr.close()

    def test_partial_json_input_cannot_bypass_response_timeout(self):
        read, write = os.pipe()
        os.write(write, b'{"type":')
        try:
            with os.fdopen(read) as source:
                ui = interview.Console('json', source, io.StringIO())
                q = dict(sessionId='s', requestId='r', phase='readiness', choices=['ready'])
                with self.assertRaisesRegex(interview.Stopped, 'timed out'):
                    ui.ask(q, .01)
        finally:
            os.close(write)

    def test_invalid_timeout_rejected(self):
        for timeout in [0, -1, float('nan'), float('inf')]:
            with self.assertRaises(ValueError): interview.Interview(self.directory, Scripted([]), timeout)


class BrowserTests(unittest.TestCase):
    def setUp(self):
        from omaphones.interview_web import Browser
        self.ui = Browser()
        self.addCleanup(self.ui.close)
        self.q = dict(type='owner-question', version=1, sessionId='s', requestId='r',
                      phase='observation', choices=['unsure'])

    def request(self, path, body=None, **headers):
        req = Request(self.ui.origin + path, data=json.dumps(body).encode() if body is not None else None,
                      headers={'X-Session-Token': self.ui.token, 'Content-Type': 'application/json', **headers})
        try:
            with urlopen(req, timeout=3) as response:
                return response.read()
        except HTTPError as error:
            error.close()
            raise

    def test_rejects_wrong_origin_token_host_paths_and_duplicate(self):
        for headers in [{'Origin': 'https://other.example'}, {'X-Session-Token': 'wrong'}, {'Host': 'other.example'}]:
            with self.assertRaises(HTTPError) as result: self.request('/state', **headers)
            self.assertEqual(result.exception.code, 403)
        with self.assertRaises(HTTPError) as result: self.request('/session.json')
        self.assertEqual(result.exception.code, 404)
        self.ui.pending = self.q
        answer = interview.make_answer(self.q, 'unsure', 'distracted', 'browser')
        with self.assertRaises(HTTPError): self.request('/answer', {**answer, 'requestId': 'old'})
        self.assertTrue(json.loads(self.request('/answer', answer))['accepted'])
        with self.assertRaises(HTTPError): self.request('/answer', answer)

    def test_stop_and_timeout_unblock_the_runner(self):
        results = []
        def wait():
            try: self.ui.ask(self.q, 2)
            except interview.Stopped as error: results.append(str(error))
        worker = threading.Thread(target=wait)
        worker.start()
        self.request('/stop', {'stop': True})
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(results), 1)
        self.ui.stopped = False
        with self.assertRaises(interview.Stopped): self.ui.ask(self.q, .01)
        self.assertIsNone(self.ui.pending)
