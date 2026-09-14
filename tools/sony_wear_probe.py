#!/usr/bin/env python3
"""Ask a Sony MDR v2 headset the SYSTEM wearing-status question directly.

sony-bridge sends this to any model without a MODELS row saying otherwise
(see NO_MODES/MODELS in sony-bridge); tools/sony_probe.py never asks it.
This is that one frame, standalone, for evidence: f2 10, then wait.

Usage: sony_wear_probe.py <address> [seconds]
"""
import os
import struct
import sys
import time

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

UUID_V2 = "956c7b26-d49a-4ba8-b03f-b17d393cb6e2"
PROFILE_PATH = "/io/github/ncr/omaphones/sonywearprobe"
START = time.monotonic()

HDR, TRL, ESC, MASK = 0x3E, 0x3C, 0x3D, 0xEF
DATA_MDR, ACK = 0x0C, 0x01


def stamp():
    return "%7.2fs" % (time.monotonic() - START)


def escape(b):
    out = bytearray()
    for x in b:
        if x in (HDR, TRL, ESC):
            out += bytes([ESC, x & MASK])
        else:
            out.append(x)
    return bytes(out)


def unescape(b):
    out, i = bytearray(), 0
    while i < len(b):
        if b[i] == ESC:
            i += 1
            out.append(b[i] | 0x10)
        else:
            out.append(b[i])
        i += 1
    return bytes(out)


def encode(dtype, seq, payload=b""):
    body = bytes([dtype, seq]) + struct.pack(">I", len(payload)) + payload
    return bytes([HDR]) + escape(body + bytes([sum(body) & 0xFF])) + bytes([TRL])


def decode(frame):
    m = unescape(frame[1:-1])
    dtype, seq = m[0], m[1]
    n = struct.unpack(">I", m[2:6])[0]
    payload, cksum = m[6:6 + n], m[6 + n]
    ok = len(payload) == n and cksum == sum(m[:6 + n]) & 0xFF
    return dtype, seq, payload, ok


class Link:
    def __init__(self, fd, plan):
        self.fd = fd
        self.buf = bytearray()
        self.seq = 0
        self.queue = list(plan)
        self.waiting = False
        GLib.io_add_watch(fd, GLib.PRIORITY_DEFAULT,
                          GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self.on_io)
        self.send(b"\x00\x00", "CONNECT_GET_PROTOCOL_INFO")

    def send(self, payload, label):
        frame = encode(DATA_MDR, self.seq, payload)
        print("%s >>> %-28s seq=%d %s" % (stamp(), label, self.seq, frame.hex()), flush=True)
        os.write(self.fd, frame)
        self.waiting = True

    def ack(self, rseq):
        os.write(self.fd, encode(ACK, 1 - rseq))

    def pump(self):
        if self.waiting or not self.queue:
            return False
        payload, label = self.queue.pop(0)
        self.send(payload, label)
        return False

    def on_io(self, fd, cond):
        if cond & (GLib.IO_HUP | GLib.IO_ERR):
            print("%s !! closed by peer" % stamp(), flush=True)
            return False
        data = os.read(fd, 4096)
        if not data:
            return False
        self.buf += data
        while True:
            s = self.buf.find(HDR)
            if s < 0:
                self.buf.clear()
                break
            e = self.buf.find(TRL, s + 1)
            if e < 0:
                del self.buf[:s]
                break
            frame = bytes(self.buf[s:e + 1])
            del self.buf[:e + 1]
            dtype, rseq, payload, ok = decode(frame)
            if dtype == ACK:
                print("%s <<< ACK seq=%d" % (stamp(), rseq), flush=True)
                self.seq = rseq
                self.waiting = False
                GLib.timeout_add(150, self.pump)
                continue
            self.ack(rseq)
            print("%s <<< type=0x%02x seq=%d payload=%s%s"
                  % (stamp(), dtype, rseq, payload.hex(), "" if ok else " [BAD CK]"),
                  flush=True)
        return True


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: sony_wear_probe.py <address> [seconds]")
    address = sys.argv[1]
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 20

    plan = [(bytes([0xF2, 0x10]), "SYSTEM_GET_STATUS wearing (f2 10)")]
    link = {}

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    class Profile(dbus.service.Object):
        @dbus.service.method("org.bluez.Profile1", in_signature="", out_signature="")
        def Release(self):
            pass

        @dbus.service.method("org.bluez.Profile1", in_signature="oha{sv}", out_signature="")
        def NewConnection(self, path, fd, properties):
            print("%s == connected" % stamp(), flush=True)
            link["l"] = Link(fd.take(), plan)

        @dbus.service.method("org.bluez.Profile1", in_signature="o", out_signature="")
        def RequestDisconnection(self, path):
            pass

    Profile(bus, PROFILE_PATH)
    manager = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"),
                             "org.bluez.ProfileManager1")
    manager.RegisterProfile(PROFILE_PATH, UUID_V2, {
        "Name": "Sony wear probe", "Role": "client",
        "RequireAuthentication": dbus.Boolean(False),
        "RequireAuthorization": dbus.Boolean(False),
    })

    def device_path():
        objects = dbus.Interface(bus.get_object("org.bluez", "/"),
                                 "org.freedesktop.DBus.ObjectManager").GetManagedObjects()
        want = address.upper()
        for path, ifaces in objects.items():
            dev = ifaces.get("org.bluez.Device1")
            if dev and str(dev.get("Address", "")).upper() == want:
                return path
        sys.exit("no paired device with address %s" % address)

    dev = device_path()
    device_obj = dbus.Interface(bus.get_object("org.bluez", dev), "org.bluez.Device1")
    device_obj.ConnectProfile(UUID_V2, reply_handler=lambda: print("%s connected profile" % stamp(), flush=True),
                              error_handler=lambda e: print("%s connect error: %s" % (stamp(), e), flush=True))

    def stop():
        print("%s == done" % stamp(), flush=True)
        loop.quit()
        return False

    GLib.timeout_add(seconds * 1000, stop)
    loop = GLib.MainLoop()
    try:
        loop.run()
    finally:
        try:
            manager.UnregisterProfile(PROFILE_PATH)
        except Exception:
            pass


if __name__ == "__main__":
    main()
