#!/usr/bin/env python3
"""Capture a QC45 session on raw RFCOMM channel 8.

Usage: bose_session.py <address> [seconds-per-step]

Release the plugin's mode bridge first. Records complete raw RX chunks at
receipt plus decoded frames. Reads the initial mode before driving observed
slots 0..3, restores that mode in finally, and verifies readback. A disconnected
device may prevent restoration; that failure is reported, never hidden.
"""
import re
import signal
import socket
import sys
import time

INIT = bytes.fromhex("00010100")
BATTERY = bytes.fromhex("02020100")
CURRENT = bytes.fromhex("1f030100")
GET_ALL = bytes.fromhex("1f010500")


def start_mode(index):
    return bytes([31, 3, 5, 2, index, 0])


class CaptureLink:
    def __init__(self, sock, wait=3.0):
        self.sock, self.wait = sock, wait
        self.buffer = bytearray()
        self.started = time.monotonic()

    def log(self, text):
        print('%7.2fs %s' % (time.monotonic() - self.started, text), flush=True)

    def exchange(self, frame, label, wait=None):
        self.log('>>> %s %s' % (label, frame.hex()))
        self.sock.sendall(frame)
        replies = []
        deadline = time.monotonic() + (self.wait if wait is None else wait)
        while time.monotonic() < deadline:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError('device disconnected')
            self.log('<<< RAW ' + chunk.hex())
            self.buffer += chunk
            while len(self.buffer) >= 4 and len(self.buffer) >= 4 + self.buffer[3]:
                size = 4 + self.buffer[3]
                raw = bytes(self.buffer[:size])
                del self.buffer[:size]
                replies.append((raw[0], raw[1], raw[2] & 15, raw[4:]))
                self.log('<<< FRAME ' + raw.hex())
        return replies


def read_mode(link, label):
    replies = link.exchange(CURRENT, label)
    modes = [p[0] for b, f, op, p in replies
             if (b, f, op) == (31, 3, 3) and len(p) == 1 and p[0] in range(4)]
    if not modes:
        raise RuntimeError('no recognized current-mode reply; refusing blind changes')
    return modes[-1]


def run_session(link):
    replies = link.exchange(INIT, 'init')
    if not any((b, f, op) == (0, 1, 3) and
               re.fullmatch(rb'[0-9]+\.[0-9]+\.[0-9]+', p)
               for b, f, op, p in replies):
        raise RuntimeError('no valid init STATUS')
    link.exchange(BATTERY, 'battery')
    initial = read_mode(link, 'initial mode')
    changed = False
    try:
        link.exchange(GET_ALL, 'mode table')
        for index in range(4):
            changed = True  # Even a failed exchange may have sent the START.
            link.exchange(start_mode(index), 'set mode %d' % index)
            actual = read_mode(link, 'readback')
            if actual != index:
                raise RuntimeError('requested mode %d, device reports %d' % (index, actual))
    finally:
        if changed:
            try:
                link.exchange(start_mode(initial), 'restore mode %d' % initial)
                actual = read_mode(link, 'restore check')
                if actual != initial:
                    raise RuntimeError('restoration readback differs from initial mode')
            except BaseException:
                link.log('!! RESTORATION FAILED; check the headset mode manually')
                raise
            link.log('== restored initial mode %d' % initial)


def main():
    if len(sys.argv) < 2:
        raise SystemExit('usage: bose_session.py <address> [seconds-per-step]')
    address = sys.argv[1].upper()
    if not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', address):
        raise SystemExit('invalid Bluetooth address')
    wait = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
    if wait <= 0:
        raise SystemExit('seconds-per-step must be positive')
    def interrupted(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    with socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM) as sock:
        sock.settimeout(10.0)
        sock.connect((address, 8))
        sock.settimeout(0.2)
        run_session(CaptureLink(sock, wait))


if __name__ == '__main__':
    main()
