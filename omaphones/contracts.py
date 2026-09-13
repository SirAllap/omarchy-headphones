"""Automatic protocol checks derived from owner input; no invented wire replies.

Merged delivery and parallel sessions are synthetic scenarios, never evidence
that hardware was exercised. Keep ordinary reviewed replay as the oracle.
"""
from copy import deepcopy
from omaphones import devices, evidence
from omaphones.api import Protocol, Report
from omaphones.registry import ROOT
from omaphones.testing import Replay


class Audit(Protocol):
    def __init__(self, adapter):
        super().__init__()
        self.adapter = adapter
        self.observations = []

    def on_event(self, event):
        effects = self.adapter.on_event(event)
        self.observations = [{key: value for key, value in effect.values.items() if effect.observed is None or key in effect.observed} for effect in effects if isinstance(effect, Report)] if event.kind == 'received' else []
        return effects


def setup(profile, directory, root):
    events = evidence.capture(directory / 'capture.jsonl', profile)
    context = next(iter(events.values()))['data']['context']
    limits = deepcopy(profile['capabilities'])
    if profile.get('batterySource') != 'bridge':
        limits.pop('battery', None)
    adapter = devices.open_protocol(profile, directory, context, root)
    return events, adapter, limits


def feed(replay, event):
    kind, value = event['direction'], event['data']
    if kind == 'rx': replay.receive(bytes.fromhex(value))
    elif kind == 'command': replay.command(value['control'], value['value'])
    elif kind == 'timer': replay.fire(value)
    elif kind == 'event': replay.event(value['kind'], value.get('value'))
    else: return
    if replay.session.exit_code not in (None, 0):
        raise ValueError('capture event %s: %s' % (event['id'], replay.session.message))


def signature(replay):
    return (list(replay.sent), deepcopy(replay.reports), replay.session.state.snapshot(), replay.session.exit_code)


def roundtrips(profile, directory, root=ROOT):
    events, adapter, limits = setup(profile, directory, root)
    audit = Audit(adapter)
    replay = Replay(audit, limits=limits)
    pending, confirmed = {}, set()
    choices = evidence.writable_cases(profile)
    try:
        for event in events.values():
            if event['direction'] == 'command':
                key, value = event['data']['control'], event['data']['value']
                if replay.session.state.accepts(key, value):
                    pending[key] = (value, event['id'], len(replay.sent))
            feed(replay, event)
            if event['direction'] == 'rx':
                for key, (value, command_id, sent_before) in list(pending.items()):
                    if len(replay.sent) > sent_before and any(key in report and type(report[key]) is type(value) and report[key] == value for report in audit.observations) and replay.session.state.values.get(key) == value:
                        confirmed.update(case for case, pair in choices.items() if pair == (key, value))
                        pending.pop(key)
        missing = sorted(set(choices) - confirmed)
        if missing:
            raise ValueError('no command followed by a wire write and explicit RX observation for: ' + ', '.join(missing) + '. Record these requests and their actual replies with tools/add-device live ' + profile['id'] + '; a label or cached value is insufficient.')
        return str(len(confirmed)) + ' declared control values confirmed by command then RX'
    finally:
        replay.session.finish(0)


def coalesced(profile, directory, root=ROOT):
    events, adapter, limits = setup(profile, directory, root)
    if next(iter(events.values()))['data']['boundary'] != 'stream':
        return 'not applicable: GATT notification boundaries must be preserved'
    original = Replay(adapter, limits=limits)
    merged = Replay(devices.open_protocol(profile, directory, next(iter(events.values()))['data']['context'], root), limits=limits)
    batches, waiting = 0, []
    def flush():
        nonlocal batches
        if not waiting: return
        # A captured TX, timer or command is a causal boundary: never merge across it.
        event = {**waiting[0], 'data': ''.join(e['data'].replace(' ', '') for e in waiting)}
        feed(merged, event)
        if len(waiting) > 1: batches += 1
        waiting.clear()
    try:
        for event in events.values():
            feed(original, event)
            if event['direction'] == 'rx': waiting.append(event)
            elif event['direction'] == 'state': continue
            else:
                flush()
                feed(merged, event)
        flush()
        if signature(original) != signature(merged):
            raise ValueError('merging adjacent RX changed writes, reports or exit status. Keep incomplete frames in a buffer and parse every complete frame in received(); reproduce with tools/add-device check ' + profile['id'])
        return str(batches) + ' RX batches merged; original writes and reports preserved' if batches else 'no causally adjacent RX in capture; model coalescing fault test remains required'
    finally:
        original.session.finish(0); merged.session.finish(0)


def isolation(profile, directory, root=ROOT):
    events, adapter, limits = setup(profile, directory, root)
    baseline = Replay(adapter, limits=limits)
    first = Replay(type(adapter)(deepcopy(adapter.model)), limits=limits)
    second = Replay(type(adapter)(deepcopy(adapter.model)), limits=limits)
    try:
        for event in events.values(): feed(baseline, event)
        expected = signature(baseline)
        for event in events.values():
            feed(first, event)
            feed(second, event)
            if signature(first) != signature(second):
                raise ValueError('sessions diverged at capture event ' + event['id'] + '. Move mutable buffers, counters and pending commands from class/module variables into __init__; reproduce with tools/add-device check ' + profile['id'])
        if signature(first) != expected:
            raise ValueError('fresh replay differs from earlier session. Remove shared mutable state and nondeterministic output; reproduce with tools/add-device check ' + profile['id'])
        return 'two interleaved sessions match the independent replay'
    finally:
        baseline.session.finish(0); first.session.finish(0); second.session.finish(0)
