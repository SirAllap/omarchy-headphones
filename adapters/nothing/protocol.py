"""Native codec extracted from nothing-bridge; existing owners still use that bridge.
New models must supply their own evidence before activation.
"""
import struct
import re
from omaphones.api import Protocol

SOF = 0x55

CTRL_WITH_CRC = 0x0160

CMD_DEVICE_INFO = 0x06

CMD_BATTERY = 0x07

CMD_ANC_GET = 0x1E

CMD_ANC_SET = 0x0F

CMD_LATENCY_GET = 0x41

CMD_LATENCY_SET = 0x40

DIR_GET = 0xC0

DIR_SET = 0xF0

DIR_ANSWER = 0x40

DIR_ACK = 0x70

DIR_EVENT = 0xE0

MODE_BYTES = {"off": 0x05, "ambient": 0x07}

LEVEL_BYTES = {"high": 0x01, "mid": 0x02, "low": 0x03, "adaptive": 0x04}

LEVEL_FROM_BYTE = {byte: name for name, byte in LEVEL_BYTES.items()}

AVAILABLE = ["off", "anc", "ambient"]

LEVELS = ["low", "mid", "high", "adaptive"]

DEFAULT_LEVEL = "adaptive"

COMPONENTS = {2: "left", 3: "right", 4: "case", 6: "headset"}

def crc16(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF

def frame(command, direction, payload=b"", operation=1):
    wire = (command & 0xFF) | ((direction & 0xFF) << 8)
    body = struct.pack("<BHHH", SOF, CTRL_WITH_CRC, wire, len(payload))
    body += bytes([operation & 0xFF]) + payload
    return body + struct.pack("<H", crc16(body))

def take_frames(buffer):
    """Whole frames from a byte buffer as (command, direction, payload)."""
    frames = []
    while True:
        try:
            start = buffer.index(SOF)
        except ValueError:
            buffer.clear()
            break
        if start:
            del buffer[:start]
        if len(buffer) < 8:
            break
        _, ctrl, wire, length = struct.unpack_from("<BHHH", buffer)
        crc_size = 2 if ctrl & 0x20 else 0
        total = 8 + length + crc_size
        if len(buffer) < total:
            break
        raw = bytes(buffer[:total])
        del buffer[:total]
        if crc_size and struct.unpack_from("<H", raw, 8 + length)[0] != crc16(raw[:8 + length]):
            # A frame that fails its check cannot be trusted; look for the next.
            continue
        frames.append((wire & 0xFF, (wire >> 8) & 0xFF, raw[8:8 + length]))
    return frames

def parse_anc(payload):
    """(mode, level) in the panel's words, or (None, None) for nothing usable.

    The mode byte sits at payload[1] behind a kind byte of 1. Some firmware
    adds a second (2, level) pair; where it does, that level wins.
    """
    if len(payload) < 2:
        return None, None
    mode_byte = payload[1]
    level = None
    for offset in range(3, len(payload) - 1, 3):
        if payload[offset] == 2 and payload[offset + 1] in LEVEL_FROM_BYTE:
            level = LEVEL_FROM_BYTE[payload[offset + 1]]
    if mode_byte == 0x07:
        return "ambient", level
    if mode_byte in (0x00, 0x05):
        return "off", level
    if mode_byte in LEVEL_FROM_BYTE:
        return "anc", level or LEVEL_FROM_BYTE[mode_byte]
    return None, level

def parse_battery(payload):
    """{"left": 85, ...} for the components present, and the charging ones."""
    levels = {}
    charging = []
    if not payload:
        return levels, charging
    count = payload[0]
    for index in range(count):
        offset = 1 + index * 2
        if offset + 1 >= len(payload):
            break
        name = COMPONENTS.get(payload[offset])
        raw = payload[offset + 1]
        level = raw & 0x7F
        if name is None or level > 100:
            continue
        levels[name] = level
        if raw & 0x80:
            charging.append(name)
    return levels, charging

class Adapter(Protocol):
    def __init__(self, model=None):
        super().__init__(model)
        self.buffer = bytearray()
        self.mode = self.level = self.latency = None
        self.battery = {}
        self.case_level = None
        self.queries_sent = False
        self.polls = 0
        self.readback_timer = None

    def send(self, command, direction=DIR_GET, payload=b''):
        self.write(frame(command, direction, payload))

    def connected(self):
        self.send(CMD_DEVICE_INFO)
        self.schedule(1500, 'queries')
        self.schedule(6500, 'deadline')

    def deadline(self):
        if self.mode is None:
            self.finish(3, 'the device did not answer the listening-mode query')

    def queries(self):
        if self.queries_sent:
            return
        self.queries_sent = True
        self.send(CMD_BATTERY)
        self.send(CMD_ANC_GET)
        self.send(CMD_LATENCY_GET)
        self.schedule(3000, 'poll')

    def poll(self):
        self.polls += 1
        self.send(CMD_ANC_GET)
        if self.polls % 5 == 0:
            self.send(CMD_BATTERY)
        self.schedule(3000, 'poll')

    def schedule_readback(self, milliseconds):
        self.cancel_timer(self.readback_timer)
        self.readback_timer = self.schedule(milliseconds, 'readback')

    def readback(self):
        self.readback_timer = None
        self.send(CMD_ANC_GET)
        self.send(CMD_LATENCY_GET)

    def received(self, data):
        self.buffer.extend(data)
        for command, direction, payload in take_frames(self.buffer):
            if direction not in (DIR_ANSWER, DIR_ACK, DIR_EVENT):
                continue
            if command == CMD_DEVICE_INFO:
                self.queries()
            elif command in (CMD_ANC_GET, 0x03) and direction != DIR_ACK:
                mode, level = parse_anc(payload)
                if mode is not None:
                    self.mode = mode
                    if level is not None:
                        self.level = level
                    self.publish(('noise.mode', 'anc.strength') if level is not None else ('noise.mode',))
            elif command in (CMD_BATTERY, 0x01) and direction != DIR_ACK:
                levels, charging = parse_battery(payload)
                if levels:
                    self.battery = {**levels, 'charging': charging}
                    if 'case' in levels:
                        self.case_level = levels['case']
                        self.battery['caseStale'] = False
                    elif self.case_level is not None:
                        self.battery.update(case=self.case_level, caseStale=True)
                    self.publish(('battery',))
            elif command == CMD_LATENCY_GET and direction == DIR_ANSWER and payload and payload[0] in (1, 2):
                self.latency = payload[0] == 1
                self.publish(('audio.low_latency',))
            elif command in (CMD_ANC_SET, CMD_LATENCY_SET):
                self.schedule_readback(400)

    def publish(self, observed):
        if self.mode is None:
            return
        values, caps = {'noise.mode': self.mode}, {'noise.mode': {'values': AVAILABLE}}
        if self.level is not None:
            values['anc.strength'] = self.level
            caps['anc.strength'] = {'values': LEVELS}
        if self.latency is not None:
            values['audio.low_latency'] = self.latency
            caps['audio.low_latency'] = {'type': 'boolean'}
        if self.battery:
            values['battery'] = self.battery
            caps['battery'] = {'readOnly': True}
        self.report(values, caps, observed=observed)

    def command(self, control, value):
        if self.mode is None:
            return
        if control == 'noise.mode':
            byte = LEVEL_BYTES[self.level or self.model['defaultAncLevel']] if value == 'anc' else MODE_BYTES.get(value)
            if byte is None:
                return
            self.send(CMD_ANC_SET, DIR_SET, bytes([1, byte, 0]))
        elif control == 'anc.strength' and value in LEVEL_BYTES:
            self.send(CMD_ANC_SET, DIR_SET, bytes([1, LEVEL_BYTES[value], 0]))
        elif control == 'audio.low_latency' and self.latency is not None and type(value) is bool:
            self.send(CMD_LATENCY_SET, DIR_SET, bytes([1 if value else 2]))
        else:
            return
        self.schedule_readback(800)
