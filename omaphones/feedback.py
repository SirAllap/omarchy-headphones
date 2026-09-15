"""File-linked diagnostics shared by terminal, JSON, summary and GitHub annotations."""
import re

# Keep fixes next to check names so every renderer carries the same instruction.
HELP = {
    'profile': ('device.json', 'Fix the manifest field named in the error; see docs/ADAPTER-API.md.'),
    'existing models': ('device.json', 'Remove the overlapping identity. Keep existing owner models on their current route.'),
    'identification': ('identity.txt', 'Save complete bluetoothctl info for this device and match its reported name and UUIDs.'),
    'adapter': ('device.json', 'Fill observed parameters and transport endpoints; keep OS operations in the shared host.'),
    'independent source recording': ('source-session/manifest.json', 'Retain BTSnoop and the owner timeline, seal with tools/owner-session, and reference reviewed packet slices from every RX/TX event. See docs/OWNER-TESTING.md.'),
    'capture and replay': ('session.json', 'Compare the failing step with capture.jsonl. Fix protocol parsing or the independently reviewed expectation; never rewrite raw replies to match code.'),
    'capability evidence': ('session.json', 'Record the missing cases on this model, then add RX-backed expectations and case labels.'),
    'command round trips': ('capture.jsonl', 'Record every declared control value and its device reply. Initial or cached state alone does not confirm a request.'),
    'coalesced delivery': ('protocol.py', 'Buffer incomplete input and process every complete frame in a received() call.'),
    'session isolation': ('protocol.py', 'Store mutable protocol state on each instance, never on a class or in a module global.'),
    'fault scenarios': ('test_adapter.py', 'Implement the named scenario with assertions against this model\'s replies. Skipped and expected-failure tests do not count.'),
    'owner hardware': ('hardware-check.json', 'Run tools/add-device live for this model against the final implementation; retain the failed report and confirm restoration.'),
    'owner integration and limits': ('owner-checks.json', 'Record the named owner observation with the current implementation hash, or explicitly document an allowed limit.'),
    'gallery': ('screenshot.png', 'Add a complete PNG of this model\'s actual panel.'),
    'protocol notes': ('protocol.md', 'Replace the placeholder with observed requests/replies, transport, firmware and untested behavior.'),
}


def decorate(result, directory, root):
    for item in result['checks']:
        filename, fix = HELP.get(item['check'], ('device.json', 'Resolve the reported problem.'))
        if filename == 'protocol.py':
            try:
                from .devices import load, resolve
                path = resolve(load(directory), directory, root)['entry']
            except (ValueError, OSError, KeyError, TypeError):
                path = directory / filename
        else:
            path = directory / filename
        location = re.search(r'capture\.jsonl line (\d+):', str(item['detail']))
        if location:
            path = directory / 'capture.jsonl'
            item['line'] = int(location[1])
        item.setdefault('file', str(path.relative_to(root)))
        item.setdefault('line', 1)
        item['fix'] = fix
        item['reproduce'] = 'tools/add-device check ' + directory.name
    return result


def escape(value, property=False):
    value = str(value).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    return value.replace(':', '%3A').replace(',', '%2C') if property else value


def annotations(reports):
    lines = []
    for report in reports:
        for item in report['checks']:
            if item['passed']: continue
            message = '%s. Fix: %s Reproduce: %s' % (item['detail'], item.get('fix', ''), item.get('reproduce', 'tools/check'))
            lines.append('::error file=%s,line=%s,title=%s::%s' % (
                escape(item.get('file', 'devices/README.md'), True), item.get('line', 1),
                escape(report['device'] + ': ' + item['check'], True), escape(message)))
    return '\n'.join(lines)
