"""Create inert device drafts; never invent protocol parameters or evidence."""
import json
import os
from pathlib import Path

from omaphones.registry import ROOT, ID, get_adapter
from omaphones import devices


def installed_root():
    return (Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'omarchy/plugins/io.github.ncr.omaphones').resolve()


def require_isolated(root):
    if root.resolve().is_relative_to(installed_root()):
        raise ValueError('work in an isolated clone; writing here reloads the active plugin')


def save(path, value):
    with path.open('x') as handle:
        json.dump(value, handle, indent=2)
        handle.write('\n')


def initialize(slug, owner, text, adapter_id=None, parameters=None, model_id=None, root=ROOT):
    require_isolated(root)
    if not ID.fullmatch(slug):
        raise ValueError('invalid device id')
    record = devices.identity(text)
    definition = {'module': 'protocol.py', 'parameters': {}, 'transport': {'kind': 'bluez-profile', 'uuidPreference': []}}
    if adapter_id:
        row = get_adapter(adapter_id, root)
        if not row or not row.get('entry'):
            raise ValueError('unknown shared protocol')
        definition = {'id': adapter_id, 'parameters': {key: None for key, spec in row.get('parameterSchema', {}).items() if spec.get('required')}}
        # Never presume another model's channel or profile UUID.
        if row['transport']['kind'] == 'rfcomm':
            definition['transport'] = {'channels': []}
        elif row['transport']['kind'] == 'bluez-profile':
            definition['transport'] = {'uuidPreference': []}
    if parameters is not None:
        definition['parameters'] = parameters
    profile = {'apiVersion': 1, 'id': slug, 'model': record['name'], 'owner': owner,
               'status': 'draft', 'match': {'names': [record['name']], 'uuids': record['uuids']},
               'adapter': definition, 'capabilities': {'noise.mode': {'type': 'enum', 'values': []}}}
    if model_id:
        profile['match']['modelId'] = model_id
    directory = root / 'devices' / slug
    directory.parent.mkdir(exist_ok=True)
    directory.mkdir()
    save(directory / 'device.json', profile)
    (directory / 'identity.txt').write_text(text)
    (directory / 'protocol.md').write_text('# ' + record['name'] + '\n\nDescribe observed requests, replies, transport, firmware and untested behavior.\n')
    save(directory / 'owner-checks.json', {'owner': owner, 'implementation': '', 'checks': {
        key: {'status': 'untested', 'evidence': ''} for key in ('shell-integration', 'reconnect', 'peer-isolation', 'charging', 'acoustics')}})
    from omaphones.contribution import FAULTS
    (directory / 'test_adapter.py').write_text('"""Synthetic damage must remain separate from observed evidence."""\nimport unittest\n\n\nclass Faults(unittest.TestCase):\n' + ''.join(
        '    def test_' + name + '(self):\n        self.fail("Add this model\'s ' + name + ' scenario")\n\n' for name in FAULTS))
    if not adapter_id:
        (directory / 'protocol.py').write_text('''"""Only observed protocol bytes. All I/O belongs to the shared host."""
from omaphones.api import Protocol


class Adapter(Protocol):
    def connected(self):
        pass

    def received(self, data):
        pass

    def command(self, control, value):
        pass
''')
    return directory
