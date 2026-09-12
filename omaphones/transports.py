"""Host-owned Bluetooth transports. No vendor frame interpretation here."""
import errno
import os
import re
import socket
import subprocess

from omaphones.api import Event
from omaphones.platform import arm_parent_death_signal


class Transport:
    def __init__(self, config, context, glib):
        self.config, self.context, self.glib = config, context, glib
        self.deliver = lambda event: None
        self.closed = False
        self.watches = set()
        self.timers = set()
        self.record = lambda direction, data: None

    def watch(self, fd, callback, conditions=None):
        flags = conditions or (self.glib.IO_IN | self.glib.IO_HUP | self.glib.IO_ERR)
        holder = []
        def run(*args):
            keep = callback(*args)
            if not keep and holder:
                self.watches.discard(holder[0])
            return keep
        token = self.glib.io_add_watch(fd, self.glib.PRIORITY_DEFAULT, flags, run)
        holder.append(token)
        self.watches.add(token)
        return token

    def later(self, ms, callback):
        holder = []
        def run():
            if holder:
                self.timers.discard(holder[0])
            if not self.closed:
                callback()
            return False
        token = self.glib.timeout_add(ms, run)
        holder.append(token)
        self.timers.add(token)
        return token

    def cancel(self, token):
        if token in self.timers:
            self.glib.source_remove(token)
            self.timers.remove(token)

    def fail(self, reason):
        if not self.closed:
            self.deliver(Event("disconnected", reason))

    def close(self):
        self.closed = True
        for token in self.watches | self.timers:
            self.glib.source_remove(token)
        self.watches.clear()
        self.timers.clear()


class StreamTransport(Transport):
    def __init__(self, *args):
        super().__init__(*args)
        self.fd = None
        self.pending = bytearray()
        self.writer = None

    def opened(self, fd):
        if self.closed or self.fd is not None:
            os.close(fd)
            return
        self.fd = fd
        os.set_blocking(fd, False)
        self.watch(fd, self.readable)
        self.deliver(Event("connected"))

    def readable(self, _fd, condition):
        # Drain readable bytes before handling a simultaneous hangup.
        if condition & self.glib.IO_IN:
            try:
                data = os.read(self.fd, 4096)
            except BlockingIOError:
                data = None
            except OSError as error:
                self.fail("the device channel closed: " + str(error))
                return False
            if data:
                self.deliver(Event("received", data))
            elif data == b"":
                self.fail("the device channel closed")
                return False
        if condition & (self.glib.IO_HUP | self.glib.IO_ERR):
            self.fail("the device channel closed")
            return False
        return not self.closed

    def write(self, data):
        if self.closed or self.fd is None:
            return
        self.pending.extend(data)
        self.flush()

    def flush(self, *_args):
        if self.closed:
            return False
        try:
            while self.pending:
                written = os.write(self.fd, self.pending)
                if not written:
                    raise OSError("zero-length channel write")
                self.record('tx', bytes(self.pending[:written]))
                del self.pending[:written]
        except BlockingIOError:
            if self.writer is None:
                self.writer = self.watch(self.fd, self.writable, self.glib.IO_OUT | self.glib.IO_HUP | self.glib.IO_ERR)
            return True
        except OSError as error:
            self.fail("the device channel closed: " + str(error))
        return False

    def writable(self, fd, condition):
        if condition & (self.glib.IO_HUP | self.glib.IO_ERR):
            self.fail("the device channel closed")
            return False
        keep = self.flush()
        if not keep:
            self.writer = None
        return keep

    def close(self):
        super().close()
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.pending.clear()


class ProfileTransport(StreamTransport):
    def start(self):
        import dbus
        import dbus.service
        self.dbus = dbus
        self.manager = None
        self.registered = False
        self.profile = None
        self.uuid = self.context.get("uuid") or self.config["uuidPreference"][0]
        if self.uuid not in self.config["uuidPreference"]:
            raise ValueError("UUID is outside this adapter's profile list")
        bus = dbus.SystemBus()
        objects = dbus.Interface(bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager").GetManagedObjects(timeout=self.config.get("replyTimeout", 5))
        path = next((p for p, interfaces in objects.items() if str(interfaces.get("org.bluez.Device1", {}).get("Address", "")).upper() == self.context["address"].upper()), None)
        if path is None:
            raise OSError("no paired device with address " + self.context["address"])
        self.device = dbus.Interface(bus.get_object("org.bluez", path), "org.bluez.Device1")
        self.manager = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"), "org.bluez.ProfileManager1")
        transport = self
        class Profile(dbus.service.Object):
            @dbus.service.method("org.bluez.Profile1", in_signature="", out_signature="")
            def Release(self):
                transport.fail("BlueZ released the device profile")

            @dbus.service.method("org.bluez.Profile1", in_signature="oha{sv}", out_signature="")
            def NewConnection(self, path, fd, properties):
                if str(path) != str(transport.device_path):
                    os.close(fd.take())
                    return
                transport.opened(fd.take())

            @dbus.service.method("org.bluez.Profile1", in_signature="o", out_signature="")
            def RequestDisconnection(self, path):
                if str(path) == str(transport.device_path):
                    transport.fail("the device channel closed")
        self.device_path = path
        self.profile_path = self.config.get("profilePath", "/io/github/ncr/omaphones/device")
        self.profile = Profile(bus, self.profile_path)
        self.manager.RegisterProfile(self.profile_path, self.uuid, {
            "Name": self.config.get("name", "Omaphones"), "Role": "client",
            "RequireAuthentication": dbus.Boolean(False), "RequireAuthorization": dbus.Boolean(False),
        }, timeout=self.config.get("replyTimeout", 5))
        self.registered = True
        self.later(self.config.get("connectDelay", 500), lambda: self.connect(0))

    def connect(self, attempt):
        if self.closed or self.fd is not None:
            return
        def failed(error):
            if self.closed or self.fd is not None:
                return
            if attempt + 1 < self.config.get("connectAttempts", 1):
                self.later(self.config.get("connectRetry", 1500), lambda: self.connect(attempt + 1))
            else:
                self.fail("cannot open the device channel: " + str(error))
        try:
            self.device.ConnectProfile(self.uuid, reply_handler=lambda: None, error_handler=failed, timeout=self.config.get("connectTimeout", 30))
        except self.dbus.DBusException as error:
            failed(error)

    def close(self):
        super().close()
        if getattr(self, "registered", False):
            try:
                self.manager.UnregisterProfile(self.profile_path, timeout=self.config.get("replyTimeout", 5))
            except self.dbus.DBusException:
                pass
            self.registered = False
        if getattr(self, "profile", None) is not None:
            self.profile.remove_from_connection()
            self.profile = None


class RfcommTransport(StreamTransport):
    """Nonblocking connection to a declared channel list, in that exact order."""
    def start(self):
        self.socket = None
        self.candidate = 0
        self.attempt = 1
        self.connect_next()

    def next_endpoint(self):
        for token in self.watches | self.timers:
            self.glib.source_remove(token)
        self.watches.clear()
        self.timers.clear()
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.socket is not None:
            self.socket.close()
        self.pending.clear()
        self.writer = None
        self.connect_next()

    def connect_next(self):
        if self.closed:
            return
        channels = self.config["channels"]
        if self.candidate >= len(channels):
            if self.attempt < self.config.get('connectAttempts', 1):
                self.attempt += 1
                self.candidate = 0
                self.later(self.config.get('connectRetry', 1500), self.connect_next)
                return
            self.fail("cannot open the declared RFCOMM channels")
            return
        channel = channels[self.candidate]
        self.candidate += 1
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        self.socket = sock
        sock.setblocking(False)
        result = sock.connect_ex((self.context["address"], channel))
        if result == 0:
            self.opened(sock.detach())
            return
        if result not in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
            sock.close()
            self.connect_next()
            return
        finished = False
        def done(_fd=None, _condition=None):
            nonlocal finished
            if finished or self.closed:
                return False
            finished = True
            self.cancel(deadline)
            self.watches.discard(watcher)
            self.glib.source_remove(watcher)
            if _fd is not None and sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0:
                self.opened(sock.detach())
            else:
                sock.close()
                self.connect_next()
            return False
        watcher = self.watch(sock.fileno(), done, self.glib.IO_OUT | self.glib.IO_HUP | self.glib.IO_ERR)
        deadline = self.later(self.config.get("connectTimeout", 10000), done)

    def close(self):
        super().close()
        if getattr(self, "socket", None) is not None:
            self.socket.close()


class GattTransport(Transport):
    ANSI = re.compile(r"\x1b\[[0-9;]*m")
    NOTIFICATION = re.compile(r"Handle Value Not/Ind: (0x[0-9a-f]+) - \((\d+) data bytes\): ([0-9a-f ]+)", re.I)

    def start(self):
        self.client = None
        self.buffer = ""
        self.ready = False
        self.discovered = False
        self.subscribed = False
        self.client = subprocess.Popen(
            ["btgatt-client", "-d", self.context["bleAddress"], "-t", self.config.get("addressType", "random")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            preexec_fn=arm_parent_death_signal)
        os.set_blocking(self.client.stdout.fileno(), False)
        self.watch(self.client.stdout.fileno(), self.readable)
        self.discovery_deadline = self.later(self.config.get("discoveryTimeout", 15000), lambda: self.fail("the BLE link never finished discovery"))

    def tell(self, line):
        if not self.closed:
            self.client.stdin.write((line + "\n").encode())
            self.client.stdin.flush()

    def write(self, data):
        self.tell("write-value %s %s" % (self.config["writeHandle"], " ".join("0x%02x" % b for b in data)))
        self.record('tx', bytes(data))

    def subscription_ready(self):
        if self.ready or self.closed:
            return
        if not self.subscribed and not self.config.get("allowUnconfirmedSubscription", False):
            self.fail("notification subscription timed out")
            return
        self.ready = True
        self.deliver(Event("connected"))

    def line(self, raw):
        line = self.ANSI.sub("", raw).strip()
        if self.closed:
            return
        if any(s in line for s in ("Failed to connect", "Device disconnected", "GATT discovery procedures failed", "Failed to register notify handler", "GATT client not initialized")):
            self.fail(line)
        elif "GATT discovery procedures complete" in line and not self.discovered:
            self.discovered = True
            self.cancel(self.discovery_deadline)
            self.tell("register-notify " + self.config["notifyHandle"])
            self.subscription_deadline = self.later(self.config.get("registerTimeout", 5000), self.subscription_ready)
        elif "Registered notify handler" in line and self.discovered:
            self.subscribed = True
            self.cancel(self.subscription_deadline)
            self.subscription_ready()
        else:
            match = self.NOTIFICATION.search(line)
            if match and int(match[1], 16) == int(self.config["notifyHandle"], 16):
                try:
                    data = bytes.fromhex(match[3])
                except ValueError:
                    return
                if len(data) == int(match[2]):
                    self.deliver(Event("received", data))

    def readable(self, fd, condition):
        if condition & self.glib.IO_IN:
            try:
                data = os.read(fd, 4096)
            except BlockingIOError:
                data = None
            except OSError:
                data = b""
            if data:
                self.buffer += data.decode("utf-8", "replace")
                while "\n" in self.buffer:
                    line, self.buffer = self.buffer.split("\n", 1)
                    self.line(line)
                if len(self.buffer) > 65536:
                    self.fail("oversized GATT client output")
            elif data == b"":
                self.fail("the BLE link closed")
                return False
        if condition & (self.glib.IO_HUP | self.glib.IO_ERR):
            self.fail("the BLE link closed")
            return False
        return not self.closed

    def close(self):
        if self.closed:
            return
        if getattr(self, "client", None) is not None:
            try:
                self.tell("quit")
            except (OSError, ValueError):
                pass
            try:
                self.client.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.client.kill()
                self.client.wait()
            self.client.stdin.close()
            self.client.stdout.close()
        super().close()


TRANSPORTS = {"bluez-profile": ProfileTransport, "rfcomm": RfcommTransport, "ble-gatt": GattTransport}
