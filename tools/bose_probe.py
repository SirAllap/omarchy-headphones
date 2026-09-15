#!/usr/bin/env python3
"""Diagnostic Profile1 probe: these UUID paths did not reach QC45 BMAP.

For a QC45 capture with state restoration, use bose_session.py.
This diagnostic sends a mode write only with explicit set:<n>; it does not
restore that write, so prefer bose_session.py for control tests.

Usage: bose_probe.py <address> [seconds] [set:<mode-index>] [uuid:...]

Registers an org.bluez.Profile1 for the BMAP UUID — the default is the
`00000000-deca-fade-deca-deafdecacaff` service the QC35/QC45 family serves;
pass `uuid:9b26d8c0-...` to probe the other Bose vendor UUID instead. BlueZ
resolves the RFCOMM channel from SDP itself, so no channel number is needed.
Lets BlueZ connect it, sends the [0.1] GET init the QC45 asks for before it
answers anything, then GETs battery [2.2] and current mode [31.3], dumps the
mode table [31.1] START (GetAll), and, with `set:<n>`, switches the mode with
START [31.3] `<n>` and reads it back. Prints every decoded frame.

The BMAP frame is four header bytes — fblock, function, flags (operator in the
low nibble), payload length — then the payload. Flags the device answers with
still parse by masking the operator with 0x0f, as bosectl does.
"""
import os
import sys
import time

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

BMAP_UUID = "00000000-deca-fade-deca-deafdecacaff"
BOSE_UUID = "9b26d8c0-a8ed-440b-95b0-c4714a518bcc"
PROFILE_PATH = "/io/github/ncr/omaphones/boseprobe"
START = time.monotonic()

OP_NAMES = {0: "SET", 1: "GET", 2: "SETGET", 3: "STATUS", 4: "ERROR",
            5: "START", 6: "RESULT", 7: "PROCESSING"}
BLOCK_NAMES = {0: "ProductInfo", 1: "Settings", 2: "Status", 31: "AudioModes"}


def stamp():
    return "%7.2fs" % (time.monotonic() - START)


def describe(op, payload):
    name = OP_NAMES.get(op & 0x0f, "op%d" % op)
    if (op & 0x0f) == 4 and payload:
        return "%s error=%02x %s" % (name, payload[0], payload.hex())
    return "%s %s" % (name, payload.hex())


class Link:
    def __init__(self, fd, plan):
        self.fd = fd
        self.buf = bytearray()
        self.queue = list(plan)
        self.waiting = False
        GLib.io_add_watch(fd, GLib.PRIORITY_DEFAULT,
                          GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self.on_io)
        # QC45 (duran) answers nothing until it has seen a GET of [0.1].
        self.send(bytes([0x00, 0x01, 0x01, 0x00]), "[0.1] init GET")
        GLib.timeout_add(4000, self.pump)

    def send(self, frame, label):
        print("%s >>> %-28s %s" % (stamp(), label, frame.hex()), flush=True)
        try:
            os.write(self.fd, frame)
        except OSError as error:
            print("%s !! write failed: %s" % (stamp(), error), flush=True)

    def pump(self):
        if self.waiting or not self.queue:
            return False
        item = self.queue.pop(0)
        frame, label = item
        self.send(frame, label)
        return bool(self.queue)

    def on_io(self, fd, cond):
        if cond & (GLib.IO_HUP | GLib.IO_ERR):
            print("%s !! closed by peer" % stamp(), flush=True)
            return False
        data = os.read(fd, 4096)
        if not data:
            return False
        print("%s <<< RAW %s" % (stamp(), data.hex()), flush=True)
        self.buf += data
        self.parse_buffer()
        return True

    def parse_buffer(self):
        while True:
            if len(self.buf) < 4:
                return
            # BMAP header is fblock, func, flags, length.
            fblock, func, flags = self.buf[0], self.buf[1], self.buf[2]
            length = self.buf[3]
            if len(self.buf) < 4 + length:
                return
            frame = bytes(self.buf[:4 + length])
            del self.buf[:4 + length]
            block = BLOCK_NAMES.get(fblock, "block%d" % fblock)
            print("%s <<< [%d.%d] %s — payload=%s"
                  % (stamp(), fblock, func,
                     describe(flags, frame[4:]),
                     frame.hex()), flush=True)


def build_plan(address, setting):
    plan = [
        (bytes([0x02, 0x02, 0x01, 0x00]), "[2.2] GET battery"),
        (bytes([0x1f, 0x03, 0x01, 0x00]), "[31.3] GET current mode"),
        (bytes([0x1f, 0x02, 0x01, 0x00]), "[31.2] GET index/count"),
        (bytes([0x1f, 0x01, 0x05, 0x00]), "[31.1] START GetAll"),
        (bytes([0x01, 0x05, 0x01, 0x00]), "[1.5] GET CNC"),
    ]
    if setting is not None:
        plan.append((bytes([0x1f, 0x03, 0x05, 0x02, setting, 0x00]),
                     "START [31.3] mode %d" % setting))
        plan.append((bytes([0x1f, 0x03, 0x01, 0x00]), "[31.3] GET readback"))
    return plan


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: bose_probe.py <address> [seconds] [set:<n>] [uuid:...]")
    address = sys.argv[1]
    rest = sys.argv[2:]
    seconds = int(rest[0]) if rest and rest[0].isdigit() else 40
    setting = None
    uuid = BMAP_UUID
    for arg in rest:
        if arg.startswith("set:"):
            setting = int(arg.split(":", 1)[1], 0)
        elif arg.startswith("uuid:"):
            uuid = arg.split(":", 1)[1].lower()
    print("%s == probing %s on %s for %ds%s"
          % (stamp(), address, uuid, seconds,
             (" (set mode %d)" % setting) if setting is not None else ""),
          flush=True)

    link = {}
    plan = build_plan(address, setting)

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    class Profile(dbus.service.Object):
        @dbus.service.method("org.bluez.Profile1", in_signature="", out_signature="")
        def Release(self):
            pass

        @dbus.service.method("org.bluez.Profile1", in_signature="oha{sv}", out_signature="")
        def NewConnection(self, path, fd, properties):
            print("%s == connected" % stamp(), flush=True)
            print("%s == props: %s" % (stamp(), properties), flush=True)
            link["l"] = Link(fd.take(), plan)

        @dbus.service.method("org.bluez.Profile1", in_signature="o", out_signature="")
        def RequestDisconnection(self, path):
            print("%s == BlueZ asked to disconnect" % stamp(), flush=True)

    Profile(bus, PROFILE_PATH)
    manager = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"),
                             "org.bluez.ProfileManager1")
    manager.RegisterProfile(PROFILE_PATH, uuid, {
        "Name": "Bose probe",
        "Role": "client",
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

    def connect(attempt=0):
        def failed(error):
            print("%s ConnectProfile: %s" % (stamp(), error.get_dbus_message()),
                  flush=True)
            if attempt < 10:
                GLib.timeout_add(1500, lambda: connect(attempt + 1))
        dbus.Interface(bus.get_object("org.bluez", dev), "org.bluez.Device1").ConnectProfile(
            uuid, reply_handler=lambda: None, error_handler=failed, timeout=30)
        return False

    GLib.timeout_add(500, connect)
    loop = GLib.MainLoop()
    GLib.timeout_add_seconds(seconds, lambda: (loop.quit(), False)[1])
    try:
        loop.run()
    finally:
        if "l" in link:
            try:
                os.close(link["l"].fd)
            except OSError:
                pass
        manager.UnregisterProfile(PROFILE_PATH)
    print("%s == done" % stamp(), flush=True)


if __name__ == "__main__":
    main()