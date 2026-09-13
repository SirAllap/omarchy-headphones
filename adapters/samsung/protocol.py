"""Native codec extracted from samsung-bridge; existing owners still use that bridge.
New models must supply their own evidence before activation.
"""
import struct
import re
from omaphones.api import Protocol

SOF = 0xFD

EOF = 0xDD

MANAGER_INFO = 136

EXTENDED_STATUS_UPDATED = 97

NOISE_CONTROLS = 120

NOISE_CONTROLS_UPDATE = 119

AVAILABLE = ["off", "anc", "ambient"]

MODE_FROM_BYTE = {0x00: "off", 0x01: "anc", 0x02: "ambient"}

MODE_TO_BYTE = {value: key for key, value in MODE_FROM_BYTE.items()}

def crc16(data):
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc & 0xFFFF

def encode(msgid, payload=b"", request=True):
    size = 1 + len(payload) + 2
    header = (size & 0x3FF) | (0x1000 if request else 0)
    body = bytes([SOF]) + struct.pack("<H", header) + bytes([msgid]) + payload
    return body + struct.pack("<H", crc16(bytes([msgid]) + payload)) + bytes([EOF])

def decode(frame):
    if len(frame) < 7 or frame[0] != SOF or frame[-1] != EOF:
        return None
    header = struct.unpack_from("<H", frame, 1)[0]
    size = header & 0x3FF
    if len(frame) != 1 + 2 + size + 1:
        return None
    msgid = frame[3]
    payload = frame[4:-3]
    if frame[-3:-1] != struct.pack("<H", crc16(bytes([msgid]) + payload)):
        return None
    return header, msgid, payload

def first_int(payload, index):
    if index >= len(payload):
        return -1
    value = payload[index]
    return value if 0 <= value <= 100 else -1

def parse_state(payload):
    if len(payload) < 13:
        return None
    mode = MODE_FROM_BYTE.get(payload[12])
    if mode is None:
        return None
    battery = {
        "left": first_int(payload, 2),
        "right": first_int(payload, 3),
        "case": first_int(payload, 7),
        "charging": [],
    }
    # Only the observed no-charging status is interpreted by the native codec.
    # Other charging bit patterns need this model owner's evidence.
    if len(payload) <= 36 or payload[36] != 0x40:
        battery.pop("charging", None)
    return {
        "modes": True,
        "mode": mode,
        "available": AVAILABLE,
        "battery": battery,
    }

class Adapter(Protocol):
    def __init__(self, model=None):
        super().__init__(model)
        self.buffer = bytearray()
        self.state = None

    def connected(self):
        self.write(encode(MANAGER_INFO, bytes([1, 1, 34])))
        self.schedule(5000, 'deadline')

    def deadline(self):
        if self.state is None:
            self.finish(3, 'the device did not answer the listening-mode query')

    def received(self, data):
        self.buffer.extend(data)
        while self.buffer:
            start = self.buffer.find(SOF)
            if start < 0:
                self.buffer.clear()
                return
            del self.buffer[:start]
            if len(self.buffer) < 7:
                return
            total = 4 + (struct.unpack_from('<H', self.buffer, 1)[0] & 0x3ff)
            if total < 7:
                del self.buffer[0]
                continue
            if len(self.buffer) < total:
                return
            packet = bytes(self.buffer[:total])
            del self.buffer[:total]
            decoded = decode(packet)
            if decoded:
                self.on_frame(*decoded)

    def on_frame(self, header, message, payload):
        if message == EXTENDED_STATUS_UPDATED:
            state = parse_state(payload)
            if state:
                self.state = state
                self.publish(('noise.mode', 'battery') if state.get('battery') else ('noise.mode',))
        elif message == NOISE_CONTROLS_UPDATE and payload and payload[0] in MODE_FROM_BYTE:
            self.state = dict(self.state or {})
            self.state['mode'] = MODE_FROM_BYTE[payload[0]]
            self.publish(('noise.mode',))

    def publish(self, observed):
        values = {'noise.mode': self.state['mode']}
        caps = {'noise.mode': {'values': AVAILABLE}}
        if self.state.get('battery'):
            values['battery'] = self.state['battery']
            caps['battery'] = {'readOnly': True}
        self.report(values, caps, observed=observed)

    def command(self, control, value):
        if self.state and control == 'noise.mode' and value in MODE_TO_BYTE:
            self.write(encode(NOISE_CONTROLS, bytes([MODE_TO_BYTE[value]])))
