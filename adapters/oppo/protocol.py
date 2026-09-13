"""Native codec extracted from oppo-bridge; existing owners still use that bridge.
New models must supply their own evidence before activation.
"""
import struct
import re
from omaphones.api import Protocol

QUERY_ANC, RET_ANC, SET_ANC, ACK_ANC = 0x010C, 0x810C, 0x0404, 0x8404

QUERY_BATTERY, RET_BATTERY = 0x0106, 0x8106

ANC_QUERY_PAYLOAD = bytes([0x01, 0x01])

MODE_FROM_PAIR = {(0x08, 0x00): "off", (0x10, 0x00): "anc", (0x00, 0x01): "ambient"}

BYTE_FROM_MODE = {"off": bytes([0x01, 0x01, 0x01]),
                  "anc": bytes([0x01, 0x01, 0x02]),
                  "ambient": bytes([0x01, 0x01, 0x04])}

AVAILABLE = ["off", "anc", "ambient"]

def encode_varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)

def build_frame(command, seq, payload=b""):
    inner = struct.pack("<H", command) + bytes([seq & 0xFF]) \
        + struct.pack("<H", len(payload)) + bytes(payload)
    return b"\xAA" + encode_varint(2 + len(inner)) + b"\x00\x00" + inner

def take_frames(buffer):
    """Whole OPOv1 frames from a byte buffer: (cmd, seq, payload) each."""
    data = bytes(buffer)
    frames = []
    index = 0
    while index < len(data):
        if data[index] != 0xAA:
            index += 1
            continue
        value = 0
        shift = 0
        pos = index + 1
        while pos < len(data):
            byte = data[pos]
            value |= (byte & 0x7F) << shift
            shift += 7
            pos += 1
            if not (byte & 0x80):
                break
        else:
            break
        if pos + 2 > len(data):
            break
        if data[pos] != 0 or data[pos + 1] != 0:
            index += 1
            continue
        if pos + 2 + 5 > len(data):
            break
        command = data[pos + 2] | (data[pos + 3] << 8)
        seq = data[pos + 4]
        paylen = data[pos + 5] | (data[pos + 6] << 8)
        end = pos + 2 + 5 + paylen
        if end > len(data):
            break
        frames.append((command, seq, data[pos + 7:end]))
        index = end
    return frames, bytearray(data[index:])

def anc_mode_of(payload):
    """The panel name for an 0x810C payload, or "" for one never seen here."""
    data = bytes(payload)
    for i in range(len(data) - 3):
        if data[i] == 0x01 and data[i + 1] == 0x01:
            pair = (data[i + 2], data[i + 3] if i + 3 < len(data) else 0)
            mode = MODE_FROM_PAIR.get(pair)
            if mode is not None:
                return mode
            return ""
    return ""

def battery_of(payload):
    """(levels, charging) for an 0x8106 payload, or (None, None) if unusable."""
    data = bytes(payload)
    if len(data) < 2:
        return None, None
    count = data[1]
    pairs = data[2:2 + count * 2]
    if len(pairs) < count * 2:
        return None, None
    levels = {}
    charging = []
    names = {1: "left", 2: "right", 3: "case"}
    for i in range(count):
        index = pairs[i * 2]
        value = pairs[i * 2 + 1]
        name = names.get(index)
        if name is None:
            continue
        levels[name] = value & 0x7F
        if value & 0x80:
            charging.append(name)
    if not levels:
        return None, None
    for name, level in levels.items():
        if level < 0 or level > 100:
            return None, None
    return levels, charging

class Adapter(Protocol):
    def __init__(self, model=None):
        super().__init__(model)
        self.buffer = bytearray()
        self.seq = 0
        self.mode = None
        self.battery = None
        self.poll_count = 0

    def next_seq(self):
        self.seq = (self.seq + 1) & 0xff
        return self.seq

    def connected(self):
        self.ask_mode()
        self.schedule(3000, 'deadline')

    def ask_mode(self):
        self.write(build_frame(QUERY_ANC, self.next_seq(), ANC_QUERY_PAYLOAD))

    def ask_battery(self):
        self.write(build_frame(QUERY_BATTERY, self.next_seq()))

    def deadline(self):
        if self.mode is None:
            self.finish(3, 'the device did not answer the listening-mode query')

    def poll(self):
        self.poll_count += 1
        if self.poll_count % 10 == 0:
            self.ask_battery()
        else:
            self.ask_mode()
        self.schedule(3000, 'poll')

    def publish(self, observed):
        if self.mode is None:
            return
        values, caps = {'noise.mode': self.mode}, {'noise.mode': {'values': AVAILABLE}}
        if self.battery:
            values['battery'] = self.battery
            caps['battery'] = {'readOnly': True}
        self.report(values, caps, observed=observed)

    def received(self, data):
        self.buffer.extend(data)
        frames, self.buffer = take_frames(self.buffer)
        for command, seq, payload in frames:
            if command == RET_ANC:
                mode = anc_mode_of(payload)
                if mode:
                    first = self.mode is None
                    self.mode = mode
                    self.publish(('noise.mode',))
                    if first:
                        self.ask_battery()
                        self.schedule(3000, 'poll')
            elif command == RET_BATTERY:
                levels, charging = battery_of(payload)
                if levels is not None:
                    self.battery = {**levels, 'charging': charging or []}
                    self.publish(('battery',))
            elif command == ACK_ANC:
                self.ask_mode()

    def command(self, control, value):
        if self.mode is not None and control == 'noise.mode' and value in BYTE_FROM_MODE:
            self.write(build_frame(SET_ANC, self.next_seq(), BYTE_FROM_MODE[value]))
