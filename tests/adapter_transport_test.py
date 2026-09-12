"""Transport lifecycle through real callbacks, with fake BlueZ/radio endpoints."""
import errno
import os
import sys
import types
import unittest
from unittest.mock import patch

from tests.adapter_api_test import Clock, GattGLib, JblReplay
from omaphones.api import Event
from omaphones.transports import ProfileTransport, RfcommTransport, StreamTransport


class TransportTests(unittest.TestCase):
    def test_stream_preserves_partial_writes_and_drains_final_read(self):
        clock = Clock(); glib = GattGLib(clock)
        transport = StreamTransport({}, {}, glib)
        rx, tx = os.pipe()
        self.addCleanup(os.close, tx)
        events = []; transport.deliver = events.append
        transport.opened(rx)
        calls = []
        def write(fd, data):
            calls.append(bytes(data))
            if len(calls) == 1:
                return 2
            if len(calls) == 2:
                raise BlockingIOError()
            return len(data)
        with patch('omaphones.transports.os.write', side_effect=write):
            transport.write(b'abcdef')
            self.assertEqual(bytes(transport.pending), b'cdef')
            writer = transport.writer
            self.assertIsNotNone(writer)
            glib.watchers[writer][2](rx, glib.IO_OUT)
            self.assertEqual(transport.pending, bytearray())
        self.assertEqual(calls, [b'abcdef', b'cdef', b'cdef'])
        os.write(tx, b'last frame')
        transport.readable(rx, glib.IO_IN | glib.IO_HUP)
        self.assertEqual([e.kind for e in events], ['connected', 'received', 'disconnected'])
        self.assertEqual(events[1].value, b'last frame')
        transport.close(); transport.close()
        self.assertEqual(glib.watchers, {})

    def test_duplicate_fd_is_closed_and_late_open_after_stop_is_discarded(self):
        glib = GattGLib(Clock()); transport = StreamTransport({}, {}, glib)
        a, aw = os.pipe(); b, bw = os.pipe(); c, cw = os.pipe()
        for fd in (aw, bw, cw):
            self.addCleanup(os.close, fd)
        transport.opened(a); transport.opened(b)
        with self.assertRaises(OSError):
            os.fstat(b)
        transport.close(); transport.opened(c)
        with self.assertRaises(OSError):
            os.fstat(c)

    def profile(self, fail=True):
        clock = Clock(); glib = GattGLib(clock)
        manager = types.SimpleNamespace(registrations=[], unregistered=[])
        manager.RegisterProfile = lambda *args, **kwargs: manager.registrations.append((args, kwargs))
        manager.UnregisterProfile = lambda *args, **kwargs: manager.unregistered.append((args, kwargs))
        device = types.SimpleNamespace(attempts=[])
        class DBusException(Exception):
            pass
        def connect(uuid, **kwargs):
            device.attempts.append(uuid)
            if fail:
                kwargs['error_handler'](DBusException('busy'))
        device.ConnectProfile = connect
        device_path = '/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF'
        objects = types.SimpleNamespace(GetManagedObjects=lambda **kwargs: {device_path: {'org.bluez.Device1': {'Address': 'AA:BB:CC:DD:EE:FF'}}})
        bus = types.SimpleNamespace(get_object=lambda name, path: {'/': objects, '/org/bluez': manager, device_path: device}[path])
        dbus = types.ModuleType('dbus'); service = types.ModuleType('dbus.service')
        class Object:
            def __init__(self, *args):
                self.removed = False
            def remove_from_connection(self):
                self.removed = True
        service.Object = Object
        service.method = lambda *args, **kwargs: lambda fn: fn
        dbus.service = service
        dbus.Interface = lambda obj, interface: obj
        dbus.SystemBus = lambda: bus
        dbus.Boolean = bool
        dbus.DBusException = DBusException
        config = {'kind': 'bluez-profile', 'uuidPreference': ['10000000-0000-0000-0000-000000000001'], 'connectDelay': 500, 'connectRetry': 1500, 'connectAttempts': 10}
        transport = ProfileTransport(config, {'address': 'AA:BB:CC:DD:EE:FF'}, glib)
        events = []
        def deliver(event):
            events.append(event)
            if event.kind == 'disconnected':
                transport.close()
        transport.deliver = deliver
        with patch.dict(sys.modules, {'dbus': dbus, 'dbus.service': service}):
            transport.start()
        self.addCleanup(transport.close)
        return transport, clock, glib, manager, device, events

    def test_profile_exact_retry_schedule_and_cleanup(self):
        transport, clock, glib, manager, device, events = self.profile()
        self.assertEqual(device.attempts, [])
        clock.advance(499); self.assertEqual(device.attempts, [])
        clock.advance(1); self.assertEqual(len(device.attempts), 1)
        clock.advance(1500 * 8); self.assertEqual(len(device.attempts), 9)
        self.assertEqual(events, [])
        clock.advance(1500)
        self.assertEqual(len(device.attempts), 10)
        self.assertEqual(events[-1].kind, 'disconnected')
        self.assertEqual(len(manager.unregistered), 1)
        self.assertEqual(clock.pending, {})
        self.assertEqual(glib.watchers, {})

    def test_profile_stop_cancels_retry_and_rejects_other_device_fd(self):
        transport, clock, glib, manager, device, events = self.profile(fail=False)
        profile = transport.profile
        rx, tx = os.pipe(); self.addCleanup(os.close, tx)
        profile.NewConnection('/another/device', types.SimpleNamespace(take=lambda: rx), {})
        with self.assertRaises(OSError):
            os.fstat(rx)
        profile.RequestDisconnection('/another/device')
        self.assertEqual(events, [])
        transport.close(); clock.advance(100000)
        self.assertEqual(device.attempts, [])
        self.assertTrue(profile.removed)
        self.assertEqual(len(manager.unregistered), 1)

    def test_gatt_partial_lines_wrong_handle_and_eof(self):
        s = JblReplay(); self.addCleanup(s.close)
        line = b'Handle Value Not/Ind: 0x000c - (10 data bytes): aa 91 07 12 01 01 02 00 03 00\n'
        with patch('omaphones.transports.os.read', return_value=line[:30]):
            s.transport.readable(-1, s.glib.IO_IN)
        self.assertEqual(s.lines, [])
        with patch('omaphones.transports.os.read', return_value=line[30:]):
            s.transport.readable(-1, s.glib.IO_IN)
        self.assertEqual(s.lines[-1]['mode'], 'anc')
        s.device('Handle Value Not/Ind: 0x0011 - (10 data bytes): aa 91 07 12 01 00 02 01 03 00')
        s.device('Handle Value Not/Ind: 0x000c - (11 data bytes): aa 91 07 12 01 00 02 01 03 00')
        self.assertEqual(len(s.lines), 1)
        s.transport.readable(-1, s.glib.IO_HUP)
        self.assertEqual(s.exit_code, 1)
        self.assertEqual(s.clock.pending, {})

    def test_rfcomm_channels_keep_order_and_stop_cancels_pending_connect(self):
        class Socket:
            def __init__(self):
                self.closed = False
            def setblocking(self, _flag):
                pass
            def connect_ex(self, endpoint):
                endpoints.append(endpoint)
                return errno.EINPROGRESS
            def fileno(self):
                return -7
            def close(self):
                self.closed = True
            def getsockopt(self, *args):
                return errno.ECONNREFUSED
        endpoints = []; sockets = []
        def socket_factory(*args):
            s = Socket(); sockets.append(s); return s
        clock = Clock(); glib = GattGLib(clock)
        transport = RfcommTransport({'channels': [15, 28], 'connectTimeout': 10}, {'address': 'AA:BB:CC:DD:EE:FF'}, glib)
        with patch('omaphones.transports.socket.socket', side_effect=socket_factory):
            transport.start()
            clock.advance(10)
            self.assertEqual([e[1] for e in endpoints], [15, 28])
            self.assertTrue(sockets[0].closed)
            transport.close()
            clock.advance(100)
        self.assertTrue(sockets[1].closed)
        self.assertEqual(len(endpoints), 2)
        self.assertEqual(glib.watchers, {})
        self.assertEqual(clock.pending, {})
