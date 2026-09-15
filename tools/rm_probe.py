#!/usr/bin/env python3
"""Raw RFCOMM probe for Bose QC45 (BMAP on channel 8 per bosectl).

Usage: rm_probe.py <address> <channel>

Opens a Bluetooth RFCOMM socket to the given channel, listens for anything the
device sends, echoes the iAP2-style DETECT greeting back if seen, then sends the
BMAP [0.1] init GET the QC45 wants before it will answer anything, and drives
the battery/mode GETs. Useful because BlueZ Profile1 connects on whichever PSV
its SDP says, which is not always what bandaids claim.
"""
import socket
import sys
import time

ADDR = sys.argv[1].upper() if len(sys.argv) > 1 else sys.exit("usage: rm_probe.py <address> <channel>")
CH = int(sys.argv[2]) if len(sys.argv) > 2 else 8
start = time.monotonic()


def stamp():
    return "%7.2fs" % (time.monotonic() - start)


def says(tag, data):
    print("%s %s %s" % (stamp(), tag, data.hex()), flush=True)


sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM,
                     socket.BTPROTO_RFCOMM)
sock.settimeout(2.0)
print("%s == connecting channel %d on %s" % (stamp(), CH, ADDR), flush=True)
sock.connect((ADDR, CH))
print("%s == connected" % stamp(), flush=True)

sock.settimeout(0.2)
greet = b""
deadline = time.monotonic() + 4
while time.monotonic() < deadline:
    try:
        data = sock.recv(4096)
    except socket.timeout:
        continue
    if not data:
        print("%s !! closed" % stamp(), flush=True)
        break
    says("<<<", data)
    greet += data

if b"\xff\x55\x02\x00\xee\x10" in greet:
    print("%s == echoing iAP2 DETECT prelude" % stamp(), flush=True)
    sock.sendall(b"\xff\x55\x02\x00\xee\x10")
    time.sleep(0.5)
    try:
        data = sock.recv(4096)
        if data:
            says("<<< after echo", data)
    except socket.timeout:
        pass

plan = [
    ("[0.1] init GET", bytes([0x00, 0x01, 0x01, 0x00])),
    ("[2.2] GET battery", bytes([0x02, 0x02, 0x01, 0x00])),
    ("[31.3] GET current mode", bytes([0x1f, 0x03, 0x01, 0x00])),
    ("[31.2] GET index/count", bytes([0x1f, 0x02, 0x01, 0x00])),
    ("[31.1] START GetAll", bytes([0x1f, 0x01, 0x05, 0x00])),
]

rx = b""
for label, frame in plan:
    says(">>> " + label, frame)
    sock.sendall(frame)
    got = b""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            data = sock.recv(4096)
        except socket.timeout:
            continue
        if not data:
            print("%s !! closed" % stamp(), flush=True)
            sys.exit(0)
        got += data
    if got:
        says("<<< reply to " + label, got)
        rx += got
    else:
        print("%s == (no reply)" % stamp(), flush=True)

sock.close()
print("%s == done" % stamp(), flush=True)