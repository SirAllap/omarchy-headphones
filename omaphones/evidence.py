"""Replay capture references through the very same Session as the live host."""
from copy import deepcopy
import json
import unittest

from omaphones import devices
from omaphones.api import Event
from omaphones.registry import ROOT, read_json
from omaphones.state import valid_value
from omaphones.testing import Replay

INPUTS = {'rx', 'command', 'timer', 'event'}


def capture(path, profile):
    events = {}
    previous = -1
    for number, line in enumerate(path.read_text().splitlines(), 1):
        try:
            event = json.loads(line)
        except ValueError as error:
            raise ValueError('capture.jsonl line %d: %s' % (number, error)) from error
        if not isinstance(event, dict) or not isinstance(event.get('id'), str) or event['id'] in events:
            raise ValueError('capture needs unique event ids')
        if type(event.get('timeNs')) is not int or event['timeNs'] < previous:
            raise ValueError('capture timestamps must be monotonic')
        previous = event['timeNs']
        if event.get('direction') not in INPUTS | {'metadata', 'tx', 'state'}:
            raise ValueError('unknown capture direction')
        if event['direction'] in ('rx', 'tx'):
            if event.get('encoding') != 'hex' or not isinstance(event.get('data'), str) or not bytes.fromhex(event['data']):
                raise ValueError('RX/TX must contain nonempty captured bytes')
        elif event.get('encoding') != 'json':
            raise ValueError('host event needs JSON encoding')
        events[event['id']] = event
    first = next(iter(events.values()), {})
    metadata = first.get('data', {})
    if first.get('direction') != 'metadata' or not isinstance(metadata, dict) or type(metadata.get('apiVersion')) is not int or metadata['apiVersion'] != 1:
        raise ValueError('capture must start with API v1 metadata')
    if metadata.get('device') != profile['id'] or metadata.get('owner') != profile['owner'] or not devices.matches(profile, metadata.get('context', {})):
        raise ValueError('capture does not identify this exact model and owner')
    if metadata.get('synthetic') or metadata.get('boundary') not in ('stream', 'gatt-notification/client-command-payload'):
        raise ValueError('capture must identify a supported real transport boundary')
    if not any(e['direction'] == 'rx' for e in events.values()) or not any(e['direction'] == 'tx' for e in events.values()):
        raise ValueError('capture needs actual RX and TX')
    return events


def independent_source(profile, directory):
    from . import owner_recording
    source = directory / 'source-session'
    summary = owner_recording.verify(source)
    metadata = owner_recording.read_json(source / 'session.json')
    if metadata['owner'] != profile['owner'] or not devices.matches(profile, {
            **metadata['device'], 'modelId': metadata.get('modelId', '')}):
        raise ValueError('independent source must identify this model and owner')
    events = capture(directory / 'capture.jsonl', profile)
    source_hash = owner_recording.digest(source / 'traffic.btsnoop')
    wire_events = [event for event in events.values() if event['direction'] in ('rx', 'tx')]
    selected = owner_recording.selected_packets(source, [part for event in wire_events for part in event.get('source', {}).get('slices', [])])
    previous = {'rx': (0, 0), 'tx': (0, 0)}
    for event in events.values():
        if event['direction'] not in ('rx', 'tx'):
            continue
        ref = event.get('source', {})
        if ref.get('sha256') != source_hash:
            raise ValueError('RX/TX event %s needs the BTSnoop source hash and reviewed packet slices' % event['id'])
        wire = owner_recording.slice_bytes(selected, ref.get('slices', []), event['direction'])
        for number, offset, length in ref['slices']:
            if (number, offset) < previous[event['direction']]:
                raise ValueError('RX/TX source bytes were reused or reordered at event ' + event['id'])
            previous[event['direction']] = (number, offset + length)
        if wire != bytes.fromhex(event['data']):
            raise ValueError('RX/TX event %s differs from its independent source' % event['id'])
    return '%d source packets; all replay RX/TX bytes have checked provenance' % summary['packets']


def writable_cases(profile):
    out = {}
    for key, spec in profile['capabilities'].items():
        if spec.get('readOnly'):
            continue
        values = spec.get('values', [False, True] if spec.get('type') == 'boolean' else [spec.get('min'), spec.get('max')])
        for value in values:
            text = str(value).lower()
            out[key + ':' + text] = (key, value)
    return out


def required_cases(profile):
    out = set(writable_cases(profile)) | {'initial', 'external-change', 'repeated', 'unsupported-command'}
    if 'wear.detected' in profile['capabilities']:
        out.update(('wear.detected:true', 'wear.detected:false'))
    if profile.get('batterySource') == 'bridge':
        out.update('battery:' + part for part in profile['capabilities']['battery']['parts'])
    return out


def validate_case(label, snapshot, profile):
    values = snapshot.get('values', {})
    choices = writable_cases(profile)
    choices.update({'wear.detected:true': ('wear.detected', True), 'wear.detected:false': ('wear.detected', False)})
    if label in choices:
        key, value = choices[label]
        if key not in values or type(values[key]) is not type(value) or values[key] != value:
            raise ValueError('case does not match observed value: ' + label)
    elif label.startswith('battery:'):
        value = values.get('battery', {}).get(label.split(':', 1)[1])
        if type(value) is not int or not 0 <= value <= 100:
            raise ValueError('case needs reported battery reading')
    elif label not in ('initial', 'external-change', 'repeated', 'unsupported-command'):
        raise ValueError('unknown coverage case: ' + label)


def execute(profile, directory, events, spec, root=ROOT, split=None):
    if type(spec.get('apiVersion')) is not int or spec['apiVersion'] != 1 or not spec.get('steps'):
        raise ValueError('session needs API v1 and steps')
    context = next(iter(events.values()))['data']['context']
    limits = deepcopy(profile['capabilities'])
    if profile.get('batterySource') != 'bridge':
        limits.pop('battery', None)
    replay = Replay(devices.open_protocol(profile, directory, context, root), limits=limits)
    expected_inputs = [key for key, e in events.items() if e['direction'] in INPUTS]
    fed, checks = [], set()
    asserted_tx = None
    state_checks = 0
    last_input = None
    pending_commands = {}
    input_had_pending = False
    before_values, before_sent = {}, []
    testcase = unittest.TestCase()
    try:
        for index, step in enumerate(spec['steps']):
            if not isinstance(step, dict) or len(set(step) - {'case', 'note'}) != 1:
                raise ValueError('one action/assertion per session step')
            if 'feed' in step:
                ref = step['feed']
                event = events[ref]
                if event['direction'] not in INPUTS:
                    raise ValueError('feed must reference captured RX or a host input')
                if len(fed) >= len(expected_inputs) or expected_inputs[len(fed)] != ref:
                    raise ValueError('replay must preserve all captured inputs in order')
                fed.append(ref)
                before_values, before_sent = deepcopy(replay.session.state.values), list(replay.sent)
                last_input = event
                input_had_pending = bool(pending_commands)
                value = event['data']
                direction = event['direction']
                if direction == 'rx':
                    raw = bytes.fromhex(value)
                    if split and split[0] == ref:
                        replay.receive(raw[:split[1]])
                        replay.receive(raw[split[1]:])
                    else:
                        replay.receive(raw)
                elif direction == 'command':
                    if replay.session.state.accepts(value['control'], value['value']):
                        pending_commands[value['control']] = value['value']
                    replay.command(value['control'], value['value'])
                    testcase.assertEqual(replay.session.state.values, before_values, 'command changed observed state')
                elif direction == 'timer':
                    replay.fire(value)
                else:
                    if value.get('kind') not in ('connected', 'disconnected', 'stop'):
                        raise ValueError('invalid lifecycle event')
                    replay.event(value['kind'], value.get('value'))
                if direction == 'rx':
                    pending_commands = {k: v for k, v in pending_commands.items() if replay.session.state.values.get(k) != v}
                if replay.session.exit_code not in (None, 0):
                    raise ValueError('owner replay failed: ' + replay.session.message)
            elif 'sent' in step:
                refs = step['sent']
                if not isinstance(refs, list) or len(refs) != len(set(refs)) or any(events[r]['direction'] != 'tx' for r in refs):
                    raise ValueError('sent assertions must reference captured TX')
                actual = [bytes.fromhex(s) for s in replay.sent]
                expected = [bytes.fromhex(events[r]['data']) for r in refs]
                if next(iter(events.values()))['data']['boundary'] == 'stream':
                    testcase.assertEqual(b''.join(actual), b''.join(expected))
                else:
                    testcase.assertEqual(actual, expected, 'GATT payload boundaries differ')
                asserted_tx = refs
            elif 'expect' in step:
                expected = step['expect']
                if not isinstance(expected, dict) or not expected.get('values'):
                    raise ValueError('fill independently reviewed state expectations')
                testcase.assertEqual(replay.session.state.snapshot(), expected)
                state_checks += 1
                label = step.get('case')
                if label:
                    validate_case(label, expected, profile)
                    if label in ('external-change', 'repeated'):
                        if not last_input or last_input['direction'] != 'rx':
                            raise ValueError('case needs actual device input')
                        if label == 'external-change' and input_had_pending:
                            raise ValueError('external-change cannot be a pending command response')
                        if label == 'external-change' and not any(
                                key in expected['values'] and before_values[key] != expected['values'][key]
                                for key in before_values if key in profile['capabilities'] and not profile['capabilities'][key].get('readOnly')):
                            raise ValueError('external-change needs a previously observed control value followed by a change')
                        equal = before_values == expected['values']
                        if equal != (label == 'repeated'):
                            raise ValueError('case contradicts observed change/repetition')
                    if label == 'unsupported-command':
                        if not last_input or last_input['direction'] != 'command' or before_sent != replay.sent:
                            raise ValueError('unsupported command must send no bytes')
                        command = last_input['data']
                        if replay.session.state.accepts(command['control'], command['value']):
                            raise ValueError('case labels a supported command as unsupported')
                    checks.add(label)
            else:
                raise ValueError('unknown session action')
        if fed != expected_inputs or not state_checks or asserted_tx != [r for r, e in events.items() if e['direction'] == 'tx']:
            raise ValueError('session must replay all inputs and assert complete TX and state')
        return checks
    except (ValueError, AssertionError, KeyError, TypeError) as error:
        error.args = ('session.json step %d: %s' % (index + 1, str(error)),)
        raise
    finally:
        replay.session.finish(0)


def verify(profile, directory, root=ROOT, fragment=True):
    events = capture(directory / 'capture.jsonl', profile)
    spec = read_json(directory / 'session.json')
    checks = execute(profile, directory, events, spec, root)
    if fragment and next(iter(events.values()))['data']['boundary'] == 'stream':
        for ref, event in events.items():
            if event['direction'] == 'rx':
                for boundary in range(1, len(bytes.fromhex(event['data']))):
                    execute(profile, directory, events, spec, root, split=(ref, boundary))
    return checks
