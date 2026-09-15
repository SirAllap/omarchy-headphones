"""Versioned owner questions shared by terminal, JSON and local browser UIs."""
import json
import math
import os
import select
import sys
import time
import uuid

from . import owner_recording as recording


CONTROL_NAMES = {'noise.mode': 'noise cancellation', 'ambient.level': 'ambient sound level',
                 'ambient.focus_on_voice': 'focus on voices'}
VALUE_NAMES = {'off': 'Off', 'anc': 'ANC', 'ambient': 'Ambient', True: 'On', False: 'Off'}


def control_label(field, value):
    shown = VALUE_NAMES.get(value, str(value)) if isinstance(value, (str, bool)) else str(value)
    return '%s to %s' % (CONTROL_NAMES.get(field, 'the selected control'), shown)


class Stopped(RuntimeError):
    """The owner stopped, input disappeared, or the response deadline expired."""


def validate_answer(answer, question, channel):
    keys = {'type', 'version', 'sessionId', 'requestId', 'answer', 'text', 'channel'}
    if not isinstance(answer, dict) or set(answer) != keys:
        raise ValueError('answer must contain exactly: ' + ', '.join(sorted(keys)))
    if (answer['type'] != 'owner-answer' or type(answer['version']) is not int or answer['version'] != 1
            or answer['sessionId'] != question['sessionId'] or answer['requestId'] != question['requestId']):
        raise ValueError('answer does not match the pending question')
    if answer['channel'] != channel or answer['answer'] not in question['choices']:
        raise ValueError('invalid channel or choice')
    if not isinstance(answer['text'], str) or len(answer['text']) > 4000:
        raise ValueError('comment must be text of at most 4000 characters')
    if ((question['phase'] == 'completion' and answer['answer'] == 'done') or answer['answer'] == 'observed') and not answer['text'].strip():
        raise ValueError('describe the performed action or actual observation in the comment')
    return answer


def make_answer(question, choice, text, channel):
    return dict(type='owner-answer', version=1, sessionId=question['sessionId'],
                requestId=question['requestId'], answer=choice, text=text, channel=channel)


class Console:
    def __init__(self, mode='terminal', input_stream=None, output=None):
        self.mode = mode
        self.channel = 'assistant-relay' if mode == 'json' else 'terminal'
        self.input = input_stream or sys.stdin
        self.output = output or sys.stdout
        self.buffer = b''
        self.eof = False

    def notify(self, event):
        if self.mode == 'json':
            print(json.dumps(event, ensure_ascii=False), file=self.output, flush=True)
        else:
            print(event.get('message', ''), file=self.output, flush=True)

    def ask(self, question, timeout):
        deadline = time.monotonic() + timeout
        self.notify(question if self.mode == 'json' else {
            'message': '\n%s\n%s\nChoices: %s\nReply with a choice, optionally followed by | your comment.' % (
                question['device'], question['question'], ' / '.join(question['choices']))})
        while True:
            while b'\n' not in self.buffer and not self.eof:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([self.input], [], [], max(0, remaining))[0]:
                    raise Stopped('owner response timed out; session incomplete')
                chunk = os.read(self.input.fileno(), 4096)
                self.eof = not chunk
                self.buffer += chunk
                if len(self.buffer) > 16384:
                    raise Stopped('owner input exceeded the response limit')
            if not self.buffer:
                raise Stopped('owner input closed; session incomplete')
            line, _, self.buffer = self.buffer.partition(b'\n')
            try:
                line = line.decode('utf-8')
                if self.mode == 'json':
                    answer = json.loads(line)
                else:
                    choice, _, comment = line.rstrip('\r\n').partition('|')
                    answer = make_answer(question, choice.strip(), comment.strip(), self.channel)
                return validate_answer(answer, question, self.channel)
            except (ValueError, TypeError) as error:
                self.notify({'type': 'input-error', 'message': str(error)})

    def close(self):
        pass


class Interview:
    def __init__(self, directory, transport, timeout=180):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('owner timeout must be a finite positive number')
        self.directory, self.transport, self.timeout = directory, transport, timeout
        meta = recording.read_json(directory / 'session.json')
        self.session_id = str(uuid.uuid4())
        self.device = meta['device']['name']
        self.observations = []
        self.skipped = []
        self.started = meta['startedMonotonicNs']
        self.event('session', 'Owner interview started', scenarioVersion=1,
                   channel=transport.channel, language='en')

    def record(self, kind, actor, obj):
        obj = {'version': 1, **obj, 'sessionId': self.session_id, 'unixNs': time.time_ns(),
               'elapsedNs': time.monotonic_ns() - self.started}
        return recording.mark(self.directory, kind, actor, json.dumps(obj, ensure_ascii=False))

    def event(self, phase, message, **fields):
        event = dict(type='interview-status', version=1, sessionId=self.session_id,
                     phase=phase, message=message, **fields)
        entry = self.record('note', 'test-refactor', event)
        self.transport.notify(event)
        return entry

    def ask(self, step, attempt, phase, question, choices, **context):
        request = dict(type='owner-question', version=1, sessionId=self.session_id,
                       requestId=str(uuid.uuid4()), stepId=step, attempt=attempt,
                       phase=phase, device=self.device, language='en', question=question,
                       choices=choices, context=context)
        self.record('note', 'test-refactor', request)
        messages = {'readiness': 'Waiting for your readiness; no test change yet.',
                    'paused': 'Paused. No test actions until you resume.',
                    'decision': 'Your observation has been saved.',
                    'completion': 'Waiting for your physical action report.'}
        if phase in messages:
            self.transport.notify(dict(type='interview-status', version=1, sessionId=self.session_id,
                                       phase=phase, message=messages[phase]))
        try:
            answer = validate_answer(self.transport.ask(request, self.timeout), request, self.transport.channel)
        except BaseException as error:
            self.record('failure', 'test-refactor', dict(type='interview-interrupted', version=1,
                        requestId=request['requestId'], message=str(error)))
            raise
        answer = {**answer, 'stepId': step, 'attempt': attempt, 'phase': phase}
        # Readiness is not a physical action; observations are not adapter replies.
        kind = 'observation' if phase == 'observation' else 'note'
        self.record(kind, 'owner', answer)
        if answer['answer'] == 'stop':
            raise Stopped('owner stopped the test')
        return answer

    def ready(self, step, attempt, question, **context):
        while True:
            answer = self.ask(step, attempt, 'readiness', question,
                              ['ready', 'pause', 'skip', 'stop'], **context)
            if answer['answer'] == 'pause':
                self.ask(step, attempt, 'paused', 'Paused. Resume when you are ready.', ['resume', 'stop'])
                continue
            if answer['answer'] == 'skip':
                self.skipped.append({'stepId': step, 'attempt': attempt, 'text': answer['text']})
                return False
            return True

    def control(self, case, field, value, client, command, profile):
        """Gate one actual transition; repeat restores its original comparison state."""
        import copy
        from .live import restore_actions
        if hasattr(client, 'refresh'):
            client.refresh()
        baseline = copy.deepcopy(client.state)
        attempt = 1
        while True:
            readiness = ('Ready to restore the comparison baseline for a repeat?' if attempt > 1 else
                         'Listen to the current sound. Ready to change %s?' % control_label(field, value))
            if not self.ready(case, attempt, readiness,
                    baseline=baseline.get('values', {}), control=field, target=value):
                return None
            if attempt == 1 and hasattr(client, 'refresh'):
                current = client.refresh()
                if current.get('values') != baseline.get('values'):
                    baseline = copy.deepcopy(current)
                    self.event('baseline', 'The device state changed while waiting. Listen to the new baseline before continuing.')
                    continue
            if attempt > 1:
                self.event('baseline', 'Restoring the comparison baseline before the repeated attempt.',
                           stepId=case, attempt=attempt)
                for cmd, key, original in restore_actions(profile, baseline):
                    client.set(cmd, key, original)
                if not self.ready(case, attempt, 'Baseline restored. Listen now; ready to repeat the change?',
                                  baseline=baseline.get('values', {}), control=field, target=value):
                    return None
            if client.state.get('values', {}).get(field) == value:
                self.event('unchanged', 'The requested value is already reported; there is no new listening comparison.',
                           stepId=case, attempt=attempt)
                return copy.deepcopy(client.state)
            self.event('action', 'Changing one control. Listen to what happens.', stepId=case, attempt=attempt)
            # A failed command is retained and goes straight to restoration. Do not
            # require an owner answer while an adapter is failing.
            state = client.set(command, field, value)
            self.event('observation', 'The device reported the requested value. Listen now.',
                       stepId=case, attempt=attempt)
            choices = ['quieter', 'louder', 'same', 'unsure', 'stop'] if field == 'noise.mode' else ['changed', 'same', 'unsure', 'stop']
            question = ('Compared with before this change, how does the background sound seem?'
                        if field == 'noise.mode' else 'What did you notice after this control change? Add any details in the comment.')
            answer = self.ask(case, attempt, 'observation', question, choices,
                              baseline=baseline.get('values', {}), control=field, target=value,
                              actionEntry=getattr(client, 'last_action_entry', None),
                              replyEntry=getattr(client, 'last_reply_entry', None))
            self.observations.append(answer)
            decision = self.ask(case, attempt, 'decision', 'Keep this observation and continue, or repeat the comparison?',
                                ['continue', 'repeat', 'stop'])
            if decision['answer'] == 'continue':
                return state
            attempt += 1

    def manual(self, step, question):
        if not self.ready(step, 1, 'Ready for a physical check? ' + question):
            return False
        start = self.record('action', 'test-refactor', dict(type='manual-instruction', stepId=step, question=question))
        answer = self.ask(step, 1, 'completion', question + ' Report completion and describe what you actually did.',
                          ['done', 'could-not-perform', 'stop'], instructionEntry=start)
        self.record('action' if answer['answer'] == 'done' else 'note', 'owner',
                    {**answer, 'type': 'manual-completion', 'instructionEntry': start})
        if answer['answer'] != 'done':
            self.skipped.append({'stepId': step, 'attempt': 1, 'text': answer['text']})
            return False
        result = self.ask(step, 1, 'observation', 'What did you independently observe? Describe it in the comment.',
                          ['observed', 'unsure', 'stop'], instructionEntry=start)
        self.observations.append(result)
        return True

    def close(self):
        self.transport.close()
