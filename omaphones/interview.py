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
    if not isinstance(answer, dict) or not keys <= set(answer) or set(answer) - keys - {'next'}:
        raise ValueError('answer needs these fields, with optional next: ' + ', '.join(sorted(keys)))
    if (answer['type'] != 'owner-answer' or type(answer['version']) is not int or answer['version'] != 1
            or answer['sessionId'] != question['sessionId'] or answer['requestId'] != question['requestId']):
        raise ValueError('answer does not match the pending question')
    if answer['channel'] != channel or answer['answer'] not in question['choices']:
        raise ValueError('invalid channel or choice')
    if not isinstance(answer['text'], str) or len(answer['text']) > 4000:
        raise ValueError('comment must be text of at most 4000 characters')
    if ((question['phase'] == 'completion' and answer['answer'] == 'done') or answer['answer'] == 'observed') and not answer['text'].strip():
        raise ValueError('describe the performed action or actual observation in the comment')
    if 'next' in answer and (question['phase'] != 'observation'
            or not question.get('context', {}).get('repeatAllowed')
            or answer['next'] not in ('continue', 'repeat', 'pause')):
        raise ValueError('next is allowed only for a repeatable observation: continue, repeat or pause')
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
        progress = question.get('context', {}).get('progress') or {}
        preview = '\nNow: %s\nNext: %s' % (
            (progress.get('current') or {}).get('title', question['stepId']),
            (progress.get('next') or {}).get('title', 'End of session')) if progress else ''
        self.notify(question if self.mode == 'json' else {
            'message': '\n%s\n%s\nChoices: %s\nReply with a choice, optionally followed by | your comment.' % (
                question['device'], question['question'], ' / '.join(question['choices'])) + preview +
                ('\nSaving starts the displayed next step. Prefix with repeat: or pause: to save and repeat or pause instead.'
                 if question.get('context', {}).get('repeatAllowed') else '')})
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
                    choice = choice.strip()
                    navigation = next((n for n in ('repeat', 'pause') if choice.startswith(n + ':')), None)
                    answer = make_answer(question, choice[len(navigation) + 1:] if navigation else choice, comment.strip(), self.channel)
                    if navigation:
                        answer['next'] = navigation
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
        self.plan = []
        self.active_step = None
        self.advance_to = None
        self.outcomes = {}
        self.started = meta['startedMonotonicNs']
        self.event('session', 'Owner interview started', scenarioVersion=1,
                   channel=transport.channel, language='en')

    def configure_plan(self, controls, extra=()):
        self.plan = [{'id': case, 'title': control_label(field, value)[0].upper() + control_label(field, value)[1:]}
                     for case, command, field, value in controls]
        self.plan.extend({'id': key, 'title': title} for key, title in extra)
        self.plan.append({'id': 'restoration', 'title': 'Restore your original settings'})
        self.event('plan', 'Session plan is ready.')

    def progress(self):
        if not self.plan:
            return None
        index = next((i for i, step in enumerate(self.plan) if step['id'] == self.active_step), None)
        return {'index': index + 1 if index is not None else 0, 'total': len(self.plan),
                'current': self.plan[index] if index is not None else None,
                'next': self.plan[index + 1] if index is not None and index + 1 < len(self.plan) else None,
                'steps': [{**step, 'status': self.outcomes.get(step['id'],
                           'active' if step['id'] == self.active_step else 'pending')} for step in self.plan]}

    def activate(self, step):
        self.active_step = step

    def complete(self, step, status='completed'):
        self.outcomes[step] = status

    def record(self, kind, actor, obj):
        obj = {'version': 1, **obj, 'sessionId': self.session_id, 'unixNs': time.time_ns(),
               'elapsedNs': time.monotonic_ns() - self.started}
        return recording.mark(self.directory, kind, actor, json.dumps(obj, ensure_ascii=False))

    def event(self, phase, message, **fields):
        if phase == 'restoration':
            for step in self.plan:
                if step['id'] != 'restoration' and step['id'] not in self.outcomes:
                    self.outcomes[step['id']] = 'interrupted' if step['id'] == self.active_step else 'not-run'
            self.activate('restoration')
        event = dict(type='interview-status', version=1, sessionId=self.session_id,
                     phase=phase, message=message, progress=self.progress(), **fields)
        entry = self.record('note', 'test-refactor', event)
        self.transport.notify(event)
        return entry

    def ask(self, step, attempt, phase, question, choices, **context):
        self.activate(step)
        context['progress'] = self.progress()
        if context.get('repeatAllowed'):
            context['startsNext'] = (self.progress() or {}).get('next')
        request = dict(type='owner-question', version=1, sessionId=self.session_id,
                       requestId=str(uuid.uuid4()), stepId=step, attempt=attempt,
                       phase=phase, device=self.device, language='en', question=question,
                       choices=choices, context=context)
        self.record('note', 'test-refactor', request)
        messages = {'readiness': 'Waiting for your readiness; no test change yet.',
                    'paused': 'Paused. No test actions until you resume.',
                    'completion': 'Waiting for your physical action report.'}
        if phase in messages:
            self.transport.notify(dict(type='interview-status', version=1, sessionId=self.session_id,
                                       phase=phase, message=messages[phase], progress=self.progress()))
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

    def check_stopped(self):
        if getattr(self.transport, 'stopped', False):
            raise Stopped('owner stopped from the browser')

    def ready(self, step, attempt, question, **context):
        self.check_stopped()
        target, self.advance_to = self.advance_to, None
        if target == step:
            self.activate(step)
            self.event('action', 'Starting the next step requested with the saved observation.', stepId=step)
            return True
        while True:
            answer = self.ask(step, attempt, 'readiness', question,
                              ['ready', 'pause', 'skip', 'stop'], **context)
            if answer['answer'] == 'pause':
                self.ask(step, attempt, 'paused', 'Paused. Resume when you are ready.', ['resume', 'stop'])
                return True
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
            if attempt == 1 and not self.ready(case, attempt,
                    'Listen to the current sound. Ready to change %s?' % control_label(field, value),
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
                    self.check_stopped()
                    client.set(cmd, key, original)
                for seconds in (2, 1):
                    self.check_stopped()
                    self.event('baseline', 'Listen to the restored baseline. Repeating in %d…' % seconds, stepId=case, attempt=attempt)
                    time.sleep(1)
            if client.state.get('values', {}).get(field) == value:
                self.event('unchanged', 'The requested value is already reported; there is no new listening comparison.',
                           stepId=case, attempt=attempt)
                return copy.deepcopy(client.state)
            self.event('action', 'Changing one control. Listen to what happens.', stepId=case, attempt=attempt)
            # A failed command is retained and goes straight to restoration. Do not
            # require an owner answer while an adapter is failing.
            self.check_stopped()
            state = client.set(command, field, value)
            self.event('observation', 'The device reported the requested value. Listen now.',
                       stepId=case, attempt=attempt)
            choices = ['quieter', 'louder', 'same', 'unsure', 'stop'] if field == 'noise.mode' else ['changed', 'same', 'unsure', 'stop']
            question = ('Compared with before this change, how does the background sound seem?'
                        if field == 'noise.mode' else 'What did you notice after this control change? Add any details in the comment.')
            answer = self.ask(case, attempt, 'observation', question, choices,
                              baseline=baseline.get('values', {}), control=field, target=value,
                              actionEntry=getattr(client, 'last_action_entry', None),
                              replyEntry=getattr(client, 'last_reply_entry', None), repeatAllowed=True)
            self.observations.append(answer)
            navigation = answer.get('next', 'continue')
            if navigation != 'repeat':
                if navigation == 'pause':
                    self.ask(case, attempt, 'paused', 'Observation saved. Resume to start the next step.', ['resume', 'stop'])
                following = (self.progress() or {}).get('next')
                self.advance_to = following['id'] if following else None
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
