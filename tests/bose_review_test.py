"""Review regressions: synthetic transport faults, real captured reply payloads.

The original owner's pin and tests remain intact. No tests touch hardware.
"""
import importlib.util
import socket
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.bose_bridge_test import bridge_module as m, Session

INIT_REPLY = bytes.fromhex('00010305312e312e30')
MODE_REPLY = bytes.fromhex('1f03030100')


class Clock:
    def __init__(self):
        self.now = 0.0
    def monotonic(self):
        return self.now


class Sock:
    def __init__(self, replies=()):
        self.replies = iter(replies)
        self.sent = []
        self.closed = False
    def settimeout(self, value):
        pass
    def setblocking(self, value):
        pass
    def sendall(self, data):
        self.sent.append(bytes(data))
    def recv(self, count):
        reply = next(self.replies, b'')
        if isinstance(reply, BaseException):
            raise reply
        return reply
    def close(self):
        self.closed = True
    def connect(self, target):
        pass


class Probe(unittest.TestCase):
    def setUp(self):
        for name, value in (("AF_BLUETOOTH", 31), ("BTPROTO_RFCOMM", 3)):
            stub = patch.object(m.socket, name, value, create=True)
            stub.start()
            self.addCleanup(stub.stop)

    def test_echo_error_and_malformed_version_do_not_initialize(self):
        for raw in ('00010100', '0001040101', '0001030100'):
            with self.subTest(raw=raw):
                b = m.Bridge('00:11:22:33:44:55')
                self.assertFalse(b._speaks_bmap(Sock([bytes.fromhex(raw)])))

    def test_fragmented_init_retains_following_partial_frame(self):
        for cut in range(1, len(INIT_REPLY)):
            b = m.Bridge('00:11:22:33:44:55')
            self.assertTrue(b._speaks_bmap(Sock([
                INIT_REPLY[:cut], INIT_REPLY[cut:] + MODE_REPLY[:3]])))
            self.assertEqual(b.buffer, MODE_REPLY[:3])
            b.buffer += MODE_REPLY[3:]
            b.sock = Sock()
            with patch.object(m, 'emit') as emitted:
                b.on_open()
                self.assertEqual(emitted.call_args.args[0]['mode'], 'anc')

    def test_reset_closes_failed_candidate_and_tries_next(self):
        first = Sock([ConnectionResetError('synthetic reset')])
        second = Sock([INIT_REPLY])
        b = m.Bridge('00:11:22:33:44:55')
        with patch.object(m.socket, 'socket', side_effect=[first, second]):
            self.assertTrue(b.connect())
        self.assertTrue(first.closed)
        self.assertFalse(second.closed)
        self.assertEqual(b.channel, m.CHANNELS[1])
        b.close()
        self.assertTrue(second.closed)

    def test_eof_during_probe_is_transient_not_silence(self):
        b = m.Bridge('00:11:22:33:44:55')
        socks = [Sock() for _ in m.CHANNELS]
        with patch.object(m.socket, 'socket', side_effect=socks), patch.object(m, 'CONNECT_ATTEMPTS', 1), patch.object(m, 'emit'):
            self.assertFalse(b.connect())
        self.assertEqual(b.exit_code, 1)
        self.assertTrue(all(s.closed for s in socks))

    def test_stop_during_connect_prevents_remaining_attempts(self):
        b = m.Bridge('00:11:22:33:44:55')
        sock = Sock()
        def connect(target):
            b.finish(0)
            raise OSError('synthetic interruption')
        sock.connect = connect
        with patch.object(m.socket, 'socket', return_value=sock) as factory:
            self.assertFalse(b.connect())
        self.assertEqual(factory.call_count, 1)
        self.assertTrue(sock.closed)
        self.assertEqual(sock.sent, [])
        self.assertEqual(b.exit_code, 0)

    def test_stop_during_probe_prevents_further_attempts(self):
        b = m.Bridge('00:11:22:33:44:55')
        sock = Sock()
        def recv(count):
            b.finish(0)
            return INIT_REPLY
        sock.recv = recv
        with patch.object(m.socket, 'socket', return_value=sock) as factory:
            self.assertFalse(b.connect())
        self.assertEqual(factory.call_count, 1)
        self.assertTrue(sock.closed)


class RunLoop(unittest.TestCase):
    def test_actual_mode_timeout_parks_after_polling(self):
        s = Session()
        clock = Clock()
        def select(*args):
            clock.now += 2
            return [], [], []
        with patch.object(s.bridge, 'connect', return_value=True), patch.object(m.time, 'monotonic', clock.monotonic), patch.object(m.select, 'select', side_effect=select):
            self.assertEqual(s.bridge.run(), 3)
        self.assertGreater(s.bridge.polls, 0)
        self.assertFalse(s.lines[-1]['modes'])

    def test_delayed_readback_runs_before_next_periodic_poll(self):
        s = Session()
        s.device('1f03030101')
        sock = Sock([MODE_REPLY])
        s.bridge.sock = sock
        clock = Clock()
        calls = []
        def select(*args):
            calls.append(1)
            if len(calls) == 1:
                clock.now = 2.1
                return [], [], []
            if len(calls) == 2:
                return [sock], [], []
            return [0], [], []
        with patch.object(s.bridge, 'connect', return_value=True), patch.object(m.time, 'monotonic', clock.monotonic), patch.object(m.select, 'select', side_effect=select), patch.object(m.os, 'read', return_value=b''):
            s.bridge.command('set anc')
            self.assertEqual(s.bridge.mode, 'ambient')
            self.assertEqual(s.bridge.run(), 0)
        self.assertEqual(s.bridge.mode, 'anc')
        self.assertEqual(sock.sent.count(m.CURRENT), 2)
        self.assertEqual(s.bridge.polls, 0)

    def test_socket_reset_through_loop_is_transient(self):
        s = Session()
        s.bridge.sock = Sock([ConnectionResetError('synthetic reset')])
        with patch.object(s.bridge, 'connect', return_value=True), patch.object(m.select, 'select', return_value=([s.bridge.sock], [], [])):
            self.assertEqual(s.bridge.run(), 1)
        self.assertIn('closed', s.lines[-1]['error'])


def load_tool(name):
    path = Path(__file__).resolve().parents[1] / 'tools' / (name + '.py')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


session_tool = load_tool('bose_session')


class SessionCapture(unittest.TestCase):
    def link(self, initial=0, fail=False):
        class Link:
            def __init__(self):
                self.mode = initial
                self.writes = []
                self.logs = []
                self.failed = False
            def log(self, text):
                self.logs.append(text)
            def exchange(self, frame, label):
                self.writes.append(frame)
                if frame == session_tool.INIT:
                    return [(0, 1, 3, b'1.1.0')]
                if frame == session_tool.CURRENT:
                    return [(31, 3, 3, bytes([self.mode]))] if self.mode is not None else []
                if frame[:3] == bytes([31, 3, 5]):
                    self.mode = frame[4]
                    if fail and not self.failed:
                        self.failed = True
                        raise KeyboardInterrupt
                return []
        return Link()

    def test_restores_actual_initial_mode_not_aware(self):
        for initial in (0, 1, 2, 3):
            link = self.link(initial)
            session_tool.run_session(link)
            self.assertEqual(link.mode, initial)
            self.assertEqual(link.writes[-2], session_tool.start_mode(initial))
            self.assertEqual(link.writes[-1], session_tool.CURRENT)

    def test_restores_after_interrupted_write(self):
        link = self.link(3, True)
        with self.assertRaises(KeyboardInterrupt):
            session_tool.run_session(link)
        self.assertEqual(link.mode, 3)

    def test_unknown_initial_mode_prevents_writes(self):
        link = self.link(None)
        with self.assertRaises(RuntimeError):
            session_tool.run_session(link)
        self.assertFalse(any(f[:3] == bytes([31, 3, 5]) for f in link.writes))

    def test_capture_retains_raw_fragments_across_exchanges(self):
        clock = Clock()
        sock = Sock()
        chunks = iter([INIT_REPLY[:3], INIT_REPLY[3:]])
        def recv(count):
            clock.now += 1
            return next(chunks)
        sock.recv = recv
        link = session_tool.CaptureLink(sock, wait=1)
        with patch.object(session_tool.time, 'monotonic', clock.monotonic), patch.object(link, 'log') as log:
            self.assertEqual(link.exchange(session_tool.INIT, 'first'), [])
            self.assertEqual(link.exchange(session_tool.CURRENT, 'second'), [(0, 1, 3, b'1.1.0')])
        self.assertEqual(sum('<<< RAW' in c.args[0] for c in log.call_args_list), 2)

    def test_failed_restoration_is_reported(self):
        link = self.link(0)
        exchange = link.exchange
        def fail_restore(frame, label):
            if label.startswith("restore"):
                raise ConnectionError("synthetic disconnect")
            return exchange(frame, label)
        link.exchange = fail_restore
        with self.assertRaises(ConnectionError):
            session_tool.run_session(link)
        self.assertTrue(any("RESTORATION FAILED" in text for text in link.logs))

    def test_profile_probe_queue_runs_to_completion(self):
        tool = load_tool('bose_probe')
        link = object.__new__(tool.Link)
        link.queue = [(b'a', 'a'), (b'b', 'b'), (b'c', 'c')]
        link.waiting = False
        sent = []
        link.send = lambda frame, label: sent.append(frame)
        while link.pump():
            pass
        self.assertEqual(sent, [b'a', b'b', b'c'])
