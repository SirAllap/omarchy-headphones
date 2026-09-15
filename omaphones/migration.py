"""Explicit owner test of an existing model through the new adapter host.

This never changes the installed plugin, routing or owner pins.
"""
import io
import json
import signal
import subprocess
import sys

from . import live, owner_recording as recording
from .devices import identity
from .registry import ROOT, get_adapter, select, transport_for, model_parameters


def prepare(adapter, info, ble_address='', model_id=''):
    context = identity(info)
    if '\tConnected: yes' not in info:
        raise ValueError('connect the headphones before starting the adapter test')
    context.update(bleAddress=ble_address, modelId=model_id)
    row = get_adapter(adapter)
    if not row or select(context['uuids'], ble_address) != adapter:
        raise ValueError('the reported device identity does not select this adapter')
    if row['transport']['kind'] == 'ble-gatt':
        from .devices import ADDRESS
        import re
        if not ADDRESS.fullmatch(ble_address) or not re.fullmatch('[0-9a-f]{6}', model_id):
            raise ValueError('GATT testing needs the device\'s observed BLE address and Fast Pair model id')
    transport = transport_for(row, context)
    return context, transport, model_parameters(row, context)


class TimelineClient:
    def __init__(self, client, directory):
        self.client, self.directory = client, directory
        self.restoring = False
        self.log_errors = []

    def __getattr__(self, name):
        return getattr(self.client, name)

    def begin_restoration(self):
        self.restoring = True

    def mark(self, kind, actor, description):
        try:
            return recording.mark(self.directory, kind, actor, description)
        except Exception as error:
            if not self.restoring:
                raise
            self.log_errors.append(str(error))
            return None

    def set(self, command, field, value):
        self.last_action_entry = self.mark('action', 'test-refactor', command)
        try:
            state = self.client.set(command, field, value)
        except BaseException as error:
            self.mark('failure', 'adapter', str(error))
            raise
        self.last_reply_entry = self.mark('note', 'adapter', json.dumps({'reported': state}))
        return state


def run(adapter, directory, *, ble_address='', model_id='', interactive=True, prompt=input, root=ROOT, interview_mode=None, owner_timeout=180):
    from .scaffold import require_isolated
    require_isolated(root)
    meta = recording.read_json(directory / 'session.json')
    if (directory / 'manifest.json').exists() or (directory / 'adapter-result.json').exists() or (directory / 'runtime.jsonl').exists():
        raise ValueError('use a new unsealed session for each adapter run')
    with (directory / 'traffic.btsnoop').open('rb') as capture:
        if capture.read(8) != b'btsnoop\0':
            raise ValueError('start independent btmon recording first')
    info = subprocess.check_output(['bluetoothctl', 'info', meta['device']['address']], text=True, timeout=15)
    context, transport, parameters = prepare(adapter, info, ble_address, model_id)
    if model_id and meta.get('modelId') != model_id:
        raise ValueError('Fast Pair model id differs from the independent session metadata')
    if {k: context[k] for k in ('name', 'address', 'uuids')} != meta['device']:
        raise ValueError('device identity changed since session creation')
    current = recording.revision(root)
    if current['codeSha256'] != meta['revision']['codeSha256']:
        raise ValueError('code changed since session creation; start a new session')
    recording.mark(directory, 'action', 'test-refactor', 'Start candidate adapter ' + adapter)
    command = [sys.executable, str(root / 'omaphones-device'), adapter,
               '--context', json.dumps(context), '--owner', meta['owner'], '--capture', str(directory / 'runtime.jsonl')]
    report = {'passed': False, 'scope': 'adapter', 'revision': current, 'context': context,
              'transport': transport, 'parameters': parameters,
              'untested': ['shell-integration', 'reconnect', 'peer-isolation', 'charging', 'acoustics']}
    client = None
    interview = None
    previous = {sig: signal.signal(sig, live.interrupt) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        if interactive and interview_mode:
            from .interview import Interview, Console
            if interview_mode == 'web':
                from .interview_web import Browser
                transport_ui = Browser()
            elif interview_mode in ('terminal', 'json'):
                transport_ui = Console(interview_mode)
            else:
                raise ValueError('unknown interview interface')
            try:
                interview = Interview(directory, transport_ui, owner_timeout)
            except BaseException:
                transport_ui.close()
                raise
        client = TimelineClient(live.Client(command), directory)
        if parameters.get('noModes'):
            try:
                client.wait(lambda state: False)
            except RuntimeError as error:
                if 'no ANC/Ambient on this model' not in str(error) or client.process.wait(timeout=5) != 3:
                    raise
                report.update(passed=True, scope='battery-only mode exclusion',
                              checks=[{'case': 'mode-control-unsupported', 'passed': True, 'reported': str(error)}])
                report['untested'].append('battery')
                return report
        initial = client.wait(lambda state: bool(state.get('capabilities', {}).get('noise.mode')))
        caps = initial['capabilities']
        # Validate all writable values before any control write.
        profile = {'id': adapter, 'owner': meta['owner'], 'capabilities': caps,
                   'batterySource': 'bridge' if 'battery' in caps else 'none'}
        if not live.initial_ready(profile, initial):
            raise ValueError('initial writable settings are incomplete; no controls sent')
        def ask(message):
            start = recording.mark(directory, 'action', 'owner', 'Begin manual step: ' + message)
            print(message, flush=True)
            action = prompt('Describe exactly what you did (do not leave blank): ').strip()
            if not action:
                raise ValueError('owner action description is required')
            recording.mark(directory, 'action', 'owner', 'Completed manual step begun at entry %d: %s' % (start, action))
            observation = prompt('What did you independently observe? Include uncertainty: ').strip()
            if not observation:
                raise ValueError('owner observation is required')
            recording.mark(directory, 'observation', 'owner', observation)
        output = io.StringIO()
        result = live.run(profile, directory, client, output, root, prompt=ask if interactive and interview is None else None,
                          implementation=current['codeSha256'], interview=interview)
        report.update(result)
    except BaseException as error:
        report.update(passed=False, error=type(error).__name__ + ': ' + str(error))
        recording.mark(directory, 'failure', 'test-refactor', report['error'])
    finally:
        try:
            if client:
                client.close()
            after = recording.revision(root)
            if after['codeSha256'] != current['codeSha256']:
                report.update(passed=False, error='code changed during the measurement')
            recording.exclusive_json(directory / 'adapter-result.json', report)
            recording.mark(directory, 'note', 'test-refactor', 'Adapter result: ' + ('passed' if report['passed'] else 'FAILED'))
        finally:
            try:
                if interview is not None:
                    try:
                        interview.event('finished', 'Adapter test ended. The assistant must stop capture and restore the previous plugin mode-control setting.',
                                        passed=report.get('passed', False), cleanupRequired=['stop-capture', 'restore-plugin-setting'])
                    finally:
                        interview.close()
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
    return report
