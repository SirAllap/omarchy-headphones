"""Independent BTSnoop evidence and a CSV timeline; no protocol adapter imports.

BTSnoop v1 record layout and Linux monitor flags follow BlueZ/Wireshark:
https://github.com/wireshark/wireshark/blob/master/wiretap/btsnoop.c
Only the capture container is decoded here. Vendor packets remain opaque.
"""
import csv
import datetime
import fcntl
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import time

EPOCH_US = 0x00dcddb30f2f8000
FIELDS = ('id', 'unix_ns', 'elapsed_ns', 'kind', 'actor', 'description')
KINDS = {'action', 'observation', 'note', 'failure'}
SOURCE_FILES = ('traffic.btsnoop', 'actions.csv', 'session.json', 'identity.txt')


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def exclusive_json(path, value):
    with Path(path).open('x') as output:
        json.dump(value, output, indent=2)
        output.write('\n')


def read_json(path):
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError('expected a JSON object: ' + str(path))
    return value


def revision(root):
    """Fingerprint executable inputs, including uncommitted work; never evidence."""
    root = Path(root)
    paths = set(root.glob('*.qml')) | set(root.glob('*-bridge'))
    paths |= {root / n for n in ('Model.js', 'gfps-reader', 'omaphones-device')}
    paths |= set((root / 'tools').glob('*'))
    paths |= set((root / 'omaphones').glob('*.py'))
    paths |= set((root / 'adapters').glob('*/*.py')) | set((root / 'adapters').glob('*/*.json'))
    paths |= set((root / 'devices').glob('*/device.json')) | set((root / 'devices').glob('*/protocol.py'))
    h = hashlib.sha256()
    for path in sorted(paths):
        if path.is_file():
            h.update(str(path.relative_to(root)).encode() + b'\0' + path.read_bytes() + b'\0')
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    dirty = subprocess.check_output(['git', 'status', '--porcelain'], cwd=root, text=True).strip()
    return {'commit': head, 'codeSha256': h.hexdigest(), 'dirty': bool(dirty)}


def packets(path):
    """Yield all records with file offsets. Reject loss/truncation, never skip it."""
    with Path(path).open('rb') as source:
        header = source.read(16)
        if len(header) != 16 or header[:8] != b'btsnoop\0':
            raise ValueError('not a BTSnoop file')
        version, link = struct.unpack('>II', header[8:])
        if version != 1 or link not in (1001, 1002, 2001):
            raise ValueError('unsupported BTSnoop version or datalink')
        previous, number = None, 0
        while True:
            offset = source.tell()
            header = source.read(24)
            if not header:
                break
            number += 1
            if len(header) != 24:
                raise ValueError('partial BTSnoop header at packet %d' % number)
            original, included, flags, drops, timestamp = struct.unpack('>IIIIq', header)
            if included != original or included > 1024 * 1024 or drops:
                raise ValueError('truncated, oversized or dropped data at packet %d' % number)
            data = source.read(included)
            if len(data) != included:
                raise ValueError('partial BTSnoop payload at packet %d' % number)
            unix_us = timestamp - EPOCH_US
            if previous is not None and unix_us < previous:
                raise ValueError('capture clock moved backwards at packet %d' % number)
            previous = unix_us
            opcode = flags & 0xffff if link == 2001 else None
            # Other monitor opcodes are retained, without guessing a direction.
            direction = ('tx' if opcode in (2, 4, 6, 18) else
                         'rx' if opcode in (3, 5, 7, 19) else None) if link == 2001 else ('rx' if flags & 1 else 'tx')
            yield {'packet': number, 'fileOffset': offset, 'unixUs': unix_us,
                   'datalink': link, 'controller': flags >> 16 if link == 2001 else None,
                   'opcode': opcode, 'flags': flags, 'direction': direction, 'data': data}


def initialize(directory, *, owner, info, firmware, scenario, controller, root, model_id=''):
    from .scaffold import require_isolated
    require_isolated(directory)
    if not re.fullmatch(r'[A-Za-z0-9-]+', owner):
        raise ValueError('owner must be a GitHub login')
    if not re.fullmatch(r'hci\d+', controller):
        raise ValueError('controller must be hci followed by its index')
    if not firmware.strip() or not scenario.strip():
        raise ValueError('firmware (or unknown) and scenario are required')
    if model_id and not re.fullmatch('[0-9a-f]{6}', model_id):
        raise ValueError('model id must be the observed six-digit Fast Pair id')
    from .devices import identity
    record = identity(info)
    directory.mkdir(parents=True, exist_ok=False)
    start = time.time_ns()
    exclusive_json(directory / 'session.json', {
        'formatVersion': 1, 'owner': owner, 'device': record, 'modelId': model_id, 'firmware': firmware,
        'scenario': scenario, 'controller': controller, 'revision': revision(root),
        'startedUnixNs': start, 'startedMonotonicNs': time.monotonic_ns(),
        'clock': 'unix_ns is CLOCK_REALTIME; elapsed_ns is CLOCK_MONOTONIC since session creation; BTSnoop timestamps convert to Unix microseconds',
        'boundary': 'Bluetooth host/controller; records every packet btmon observes on the selected controller',
        'environment': {'kernel': __import__('platform').release(),
                        'btmon': subprocess.check_output(['btmon', '--version'], text=True).strip()},
    })
    (directory / 'identity.txt').write_text(info)
    with (directory / 'actions.csv').open('x', newline='') as output:
        csv.writer(output).writerow(FIELDS)
    return record


def mark(directory, kind, actor, description):
    from .scaffold import require_isolated
    require_isolated(directory)
    if (directory / 'actions.csv').is_symlink():
        raise ValueError('action log must be a regular session file, not a symlink')
    if kind not in KINDS or not actor.strip() or not description.strip():
        raise ValueError('kind, actor and a concrete description are required')
    meta = read_json(directory / 'session.json')
    with (directory / 'actions.csv').open('r+', newline='') as output:
        fcntl.flock(output, fcntl.LOCK_EX)
        if (directory / 'manifest.json').exists():
            raise ValueError('session is sealed; preserve the original evidence')
        rows = list(csv.DictReader(output))
        row = (len(rows) + 1, time.time_ns(), time.monotonic_ns() - meta['startedMonotonicNs'], kind, actor, description)
        output.seek(0, 2)
        csv.writer(output).writerow(row)
        output.flush()
    return row[0]


def inspect(directory):
    meta = read_json(directory / 'session.json')
    if meta.get('formatVersion') != 1:
        raise ValueError('unsupported session format version')
    from .devices import identity
    if identity((directory / 'identity.txt').read_text()) != meta['device']:
        raise ValueError('session identity differs from identity.txt')
    with (directory / 'actions.csv').open(newline='') as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != list(FIELDS):
            raise ValueError('unexpected action CSV columns')
        rows = list(reader)
    kinds, last, owner_observed = set(), -1, False
    for number, row in enumerate(rows, 1):
        elapsed, unix = int(row['elapsed_ns']), int(row['unix_ns'])
        if int(row['id']) != number or elapsed < last or elapsed < 0:
            raise ValueError('action ids/times are out of order')
        if abs(unix - meta['startedUnixNs'] - elapsed) > 1_000_000_000:
            raise ValueError('action clock moved; document and realign before deriving timing tests')
        if row['kind'] not in KINDS or not row['actor'].strip() or not row['description'].strip():
            raise ValueError('incomplete action/observation record')
        kinds.add(row['kind'])
        owner_observed |= row['kind'] == 'observation' and row['actor'] == 'owner'
        last = elapsed
    if not {'action', 'observation'} <= kinds or not owner_observed:
        raise ValueError('record both performed actions and independent owner observations')
    count, directions, first, end = 0, set(), None, None
    for packet in packets(directory / 'traffic.btsnoop'):
        count += 1
        directions.add(packet['direction'])
        first = packet['unixUs'] if first is None else first
        end = packet['unixUs']
    if not count or not {'rx', 'tx'} <= directions:
        raise ValueError('capture needs both incoming and outgoing traffic')
    if first * 1000 < meta['startedUnixNs'] - 1_000_000_000:
        raise ValueError('capture predates this session; do not attach an unrelated recording')
    return {'packets': count, 'firstUnixUs': first, 'lastUnixUs': end, 'timelineEntries': len(rows)}


def seal(directory, notes):
    from .scaffold import require_isolated
    require_isolated(directory)
    if not notes.strip():
        raise ValueError('describe scenario completion and any omissions or limits')
    # The same lock prevents a simultaneous annotation from racing the manifest.
    with (directory / 'actions.csv').open() as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = {name: digest(directory / name) for name in SOURCE_FILES}
        summary = inspect(directory)
        after = {name: digest(directory / name) for name in SOURCE_FILES}
        if before != after:
            raise ValueError('source files changed while checking; stop btmon before sealing')
        exclusive_json(directory / 'manifest.json', {
            'formatVersion': 1, 'sha256': after, 'summary': summary,
            'sealedUtc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'ownerNotes': notes, 'scope': 'file integrity and timeline structure; not hardware approval or proof of scenario coverage',
        })
    return summary


def verify(directory):
    manifest = read_json(directory / 'manifest.json')
    if manifest.get('formatVersion') != 1 or set(manifest.get('sha256', {})) != set(SOURCE_FILES):
        raise ValueError('invalid source manifest')
    for name, expected in manifest['sha256'].items():
        if digest(directory / name) != expected:
            raise ValueError('source changed after sealing: ' + name)
    summary = inspect(directory)
    if summary != manifest['summary']:
        raise ValueError('source summary differs from the sealed manifest')
    return summary


def selected_packets(directory, slices):
    """Read only referenced packets into memory, in one pass through the file."""
    wanted = {}
    for spec in slices:
        if len(spec) != 3 or any(type(v) is not int for v in spec) or spec[0] < 1 or spec[1] < 0 or spec[2] < 1:
            raise ValueError('slice must be [packet number, zero-based byte offset, length]')
        wanted[spec[0]] = None
    for packet in packets(directory / 'traffic.btsnoop'):
        if packet['packet'] in wanted:
            wanted[packet['packet']] = packet
    return wanted


def slice_bytes(wanted, slices, direction):
    result = bytearray()
    previous = (0, 0)
    for number, offset, length in slices:
        packet = wanted[number]
        if packet is None or packet['direction'] != direction:
            raise ValueError('missing packet or wrong TX/RX direction')
        if (number, offset) < previous or offset + length > len(packet['data']):
            raise ValueError('overlapping, reordered or out-of-bounds packet slice')
        previous = (number, offset + length)
        result.extend(packet['data'][offset:offset + length])
    if not result:
        raise ValueError('at least one packet slice is required')
    return bytes(result)


def extract(directory, slices, direction):
    """Explicit reviewed packet slices; no current vendor decoder involved."""
    verify(directory)
    return slice_bytes(selected_packets(directory, slices), slices, direction)
