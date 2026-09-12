"""Native codec extracted from xiaomi-bridge; existing owners still use that bridge.
New models must supply their own evidence before activation.
"""
import struct
import re
from omaphones.api import Protocol

GAIA_VENDOR = 0x000A

DEVICE_VENDOR = 0x001D

HANDSHAKE, HANDSHAKE_RET = 0x0300, 0x8300

GET_MODE, RET_MODE, SET_MODE, ACK_MODE = 0x1003, 0x1103, 0x1004, 0x1104

MODE_FROM_BYTE = {0x00: "off", 0x01: "anc", 0x02: "ambient"}

BYTE_FROM_MODE = {name: byte for byte, name in MODE_FROM_BYTE.items()}

AVAILABLE = ["off", "anc", "ambient"]

FORBIDDEN_SET = frozenset({0x04})

def frame(version, vendor, command, payload=b""):
    return bytes([0xFF, version & 0xFF, 0x00, len(payload)]) + struct.pack(
        ">HH", vendor, command) + payload

def take_frames(buffer):
    """Whole Compact GAIA frames from a byte buffer, leftover bytes kept."""
    data = bytes(buffer)
    frames = []
    index = 0
    while index < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        if index + 8 > len(data):
            break
        flags = data[index + 2]
        # This device answered flags=0, compact length. Anything else is not
        # a frame we can trust, so skip the 0xFF rather than guess.
        if flags != 0:
            index += 1
            continue
        length = data[index + 3]
        need = 8 + length
        if index + need > len(data):
            break
        version = data[index + 1]
        vendor, command = struct.unpack(">HH", data[index + 4:index + 8])
        payload = data[index + 8:index + need]
        frames.append((version, vendor, command, payload))
        index += need
    return frames, bytearray(data[index:])

class Adapter(Protocol):
    def __init__(self, model=None):
        super().__init__(model)
        self.buffer = bytearray()
        self.version = 3
        self.handshake_seen = False
        self.mode = None

    def connected(self):
        self.handshake(1)

    def handshake(self, attempt):
        if self.handshake_seen:
            return
        if attempt > 3:
            self.finish(1, 'the device did not answer the handshake')
            return
        self.write(frame(self.version, GAIA_VENDOR, HANDSHAKE))
        self.schedule(2000, 'handshake', attempt + 1)

    def ask_mode(self):
        self.write(frame(self.version, DEVICE_VENDOR, GET_MODE))

    def deadline(self):
        if self.mode is None:
            self.finish(3, 'the device did not answer the listening-mode query')

    def poll(self):
        self.ask_mode()
        self.schedule(2500, 'poll')

    def received(self, data):
        self.buffer.extend(data)
        frames, self.buffer = take_frames(self.buffer)
        for version, vendor, command, payload in frames:
            if 1 <= version <= 4:
                self.version = version
            if vendor == GAIA_VENDOR and command == HANDSHAKE_RET and not self.handshake_seen:
                self.handshake_seen = True
                self.ask_mode()
                self.schedule(3000, 'deadline')
            elif vendor == DEVICE_VENDOR and command == ACK_MODE:
                self.ask_mode()
            elif vendor == DEVICE_VENDOR and command == RET_MODE and payload and payload[0] in MODE_FROM_BYTE:
                first = self.mode is None
                self.mode = MODE_FROM_BYTE[payload[0]]
                self.report({'noise.mode': self.mode}, {'noise.mode': {'values': AVAILABLE}})
                if first:
                    self.schedule(2500, 'poll')

    def command(self, control, value):
        if self.mode is not None and control == 'noise.mode' and value in BYTE_FROM_MODE:
            self.write(frame(self.version, DEVICE_VENDOR, SET_MODE, bytes([BYTE_FROM_MODE[value]])))
