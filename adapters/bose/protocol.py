"""Native codec extracted from bose-bridge; existing owners still use that bridge.
New models must supply their own evidence before activation.
"""
import struct
import re
from omaphones.api import Protocol

OP_STATUS, OP_ERROR, OP_START = 3, 4, 5

INIT = bytes([0x00, 0x01, 0x01, 0x00])

BATTERY = bytes([0x02, 0x02, 0x01, 0x00])

CURRENT = bytes([0x1f, 0x03, 0x01, 0x00])

def start_mode(index):
    """START [31.3]: switch to the mode index, no voice prompt."""
    return bytes([0x1f, 0x03, OP_START, 0x02, index, 0x00])

MODE_BY_IDX = {0: "anc", 1: "ambient"}

AVAILABLE = ["anc", "ambient"]

def take_frames(buffer):
    """Every whole BMAP frame the buffer holds, leaving the rest.

    One BMAP packet is [fblock, func, flags, length] + `length` payload
    bytes, so the length field is the framer: a packet whose payload has not
    arrived yet stays in the buffer and a frame that parses is exact.
    """
    frames = []
    while len(buffer) >= 4:
        fblock, func = buffer[0], buffer[1]
        op = buffer[2] & 0x0F
        length = buffer[3]
        if len(buffer) < 4 + length:
            break
        payload = bytes(buffer[4:4 + length])
        frames.append((fblock, func, op, payload))
        del buffer[:4 + length]
    return frames

def parse_battery(payload):
    """The level out of a [2.2] STATUS payload, or None.

    The QC45 answered four bytes — the level first — and the model has no
    per-earpiece batteries, so only the first byte is read, and only as a
    whole number in range, the way nothing-bridge drops a level over 100.
    """
    if len(payload) < 1:
        return None
    level = payload[0]
    if level < 0 or level > 100:
        return None
    return level

class Adapter(Protocol):
    def __init__(self, model=None):
        super().__init__(model)
        self.buffer = bytearray()
        self.probing = True
        self.mode = self.battery = None
        self.polls = 0
        self.probe_timer = self.readback_timer = None

    def connected(self):
        self.buffer.clear()
        self.probing = True
        self.write(INIT)
        self.probe_timer = self.schedule(1000, 'probe_timeout')

    def probe_timeout(self):
        if self.probing:
            self.next_endpoint()

    def opened(self):
        self.probing = False
        self.cancel_timer(self.probe_timer)
        self.write(BATTERY)
        self.write(CURRENT)
        self.schedule(4000, 'poll')
        self.schedule(10000, 'deadline')

    def deadline(self):
        if self.mode is None:
            self.finish(3, 'the device did not answer the listening-mode query')

    def poll(self):
        self.polls += 1
        self.write(CURRENT)
        if self.polls % 4 == 0:
            self.write(BATTERY)
        self.schedule(4000, 'poll')

    def readback(self):
        self.readback_timer = None
        self.write(CURRENT)

    def received(self, data):
        self.buffer.extend(data)
        for block, function, op, payload in take_frames(self.buffer):
            if self.probing:
                if (block, function, op) == (0, 1, OP_STATUS) and re.fullmatch(rb'[0-9]+\.[0-9]+\.[0-9]+', payload):
                    self.opened()
                continue
            if (block, function, op) == (2, 2, OP_STATUS):
                level = parse_battery(payload)
                if level is not None:
                    self.battery = level
                    self.publish()
            elif (block, function, op) == (31, 3, OP_STATUS) and len(payload) == 1 and payload[0] in MODE_BY_IDX:
                self.mode = MODE_BY_IDX[payload[0]]
                self.publish()

    def publish(self):
        if self.mode is None:
            return
        values, caps = {'noise.mode': self.mode}, {'noise.mode': {'values': AVAILABLE}}
        if self.battery is not None:
            values['battery'] = {'headset': self.battery, 'charging': []}
            caps['battery'] = {'readOnly': True}
        self.report(values, caps)

    def command(self, control, value):
        if self.mode is not None and control == 'noise.mode' and value in AVAILABLE:
            self.write(start_mode(0 if value == 'anc' else 1))
            self.cancel_timer(self.readback_timer)
            self.readback_timer = self.schedule(2000, 'readback')
