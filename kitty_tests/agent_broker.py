"""Native-free broker transport checks: python -m unittest kitty_tests.agent_broker."""

import json
import os
import socket
import stat
import time
import unittest
from types import SimpleNamespace

from kitty.agent_broker import Broker


class BrokerTest(unittest.TestCase):
    def setUp(self):
        self.window = SimpleNamespace(id=7)
        self.boss = SimpleNamespace(window_id_map={7: self.window})
        self.removed = []
        self.broker = Broker(self.boss, lambda *a: 42, self.removed.append)
        self.broker.identity = lambda w: ('opencode', 'ses_test123', (1, 2))
        self.reports = []
        self.broker.report = lambda w, p: self.reports.append((w, p))
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.broker.close()

    def connect(self, payload):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.clients.append(client)
        client.connect(self.broker.path)
        client.sendall((json.dumps(payload) + '\n').encode())
        return client

    def pump(self, predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            self.broker.drain()
            if predicate():
                return
            time.sleep(0.02)
        self.fail('broker did not reach expected state')

    def registration(self, token):
        return dict(type='register', protocol_version=1, token=token,
                    agent='opencode', session_id='ses_test123', capabilities=['cancel'])

    def test_socket_and_disconnect(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.broker.directory).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.broker.path).st_mode), 0o600)
        token, env = self.broker.prepare()
        self.assertIn('KITTY_AGENT_BROKER_TOKEN=' + token, env)
        self.broker.bind(token, self.window)
        client = self.connect(self.registration(token))
        self.pump(lambda: len(self.reports) == 1)
        self.assertEqual(self.reports[0][1]['capabilities'], [])
        self.assertNotIn('token', self.reports[0][1])
        client.close()
        self.pump(lambda: self.broker.tokens[token][2] is None)
        self.assertEqual(self.reports[-1][1]['type'], 'disconnect')
        self.connect(self.registration(token))
        self.pump(lambda: self.broker.tokens[token][2] is not None)

    def test_process_replacement_revokes(self):
        token, _ = self.broker.prepare()
        self.broker.bind(token, self.window)
        self.connect(self.registration(token))
        self.pump(lambda: bool(self.reports))
        self.broker.identity = lambda w: ('opencode', 'ses_test123', (1, 3))
        self.broker.drain()
        self.assertNotIn(token, self.broker.tokens)
        self.assertEqual(self.reports[-1][1]['type'], 'disconnect')

    def test_observation_only_transport(self):
        self.assertFalse(hasattr(self.broker, 'send'))
        token, _ = self.broker.prepare()
        self.broker.bind(token, self.window)
        client = self.connect(self.registration(token))
        self.pump(lambda: self.broker.tokens[token][2] is not None)
        binding = self.broker.tokens[token][2]
        self.assertNotIn('out', binding)
        self.assertEqual(self.reports[-1][1]['capabilities'], [])
        client.settimeout(0.05)
        with self.assertRaises(socket.timeout):
            client.recv(1)
        client.sendall((json.dumps(dict(type='event', event=dict(
            kind='session.idle', session_id='ses_test123'))) + '\n').encode())
        self.pump(lambda: len(self.reports) == 2)
        self.assertEqual(self.reports[-1][1]['event']['kind'], 'session.idle')

    def test_unknown_session_and_kind(self):
        for event in (dict(kind='session.status', session_id='child', parent_session_id='ses_test123'),
                      dict(kind='unknown', session_id='ses_test123')):
            token, _ = self.broker.prepare()
            self.broker.bind(token, self.window)
            client = self.connect(self.registration(token))
            self.pump(lambda: self.broker.tokens[token][2] is not None)
            count = len(self.reports)
            client.sendall((json.dumps(dict(type='event', event=event)) + '\n').encode())
            self.pump(lambda: self.broker.tokens[token][2] is None)
            self.assertEqual(len(self.reports), count + 1)
            self.assertEqual(self.reports[-1][1]['type'], 'disconnect')

    def test_wrong_token(self):
        self.connect(self.registration('wrong-token'))
        time.sleep(0.08)
        self.pump(lambda: not self.broker.clients)
        self.assertEqual(self.reports, [])

    def test_actual_reporter_gap_and_snapshot(self):
        token, _ = self.broker.prepare()
        self.broker.bind(token, self.window)
        c = self.connect(self.registration(token))
        self.pump(lambda: self.broker.tokens[token][2] is not None)
        self.broker.receive(c._broker_client if hasattr(c, '_broker_client') else next(x for x in self.broker.clients if x['token'] == token),
                            {'type': 'event', 'event': {'kind': 'gap', 'session_id': 'ses_test123',
                             'payload': {'reason': 'reconnect', 'partial': True, 'children': 'unknown'}}})
        self.broker.receive(next(x for x in self.broker.clients if x['token'] == token),
                            {'type': 'event', 'event': {'kind': 'session.status', 'session_id': 'ses_test123',
                             'payload': {'status': 'idle', 'snapshot': True, 'partial': True}}})
        self.assertEqual(len(self.reports), 3)
        self.assertIsNotNone(self.broker.tokens[token][2])

    def test_boolean_version_rejected(self):
        token, _ = self.broker.prepare()
        self.broker.bind(token, self.window)
        payload = self.registration(token)
        payload['protocol_version'] = True
        self.connect(payload)
        time.sleep(0.08)
        self.pump(lambda: not self.broker.clients)
        self.assertEqual(self.reports, [])

    def test_session_switch_reconnect(self):
        token, _ = self.broker.prepare()
        self.broker.bind(token, self.window)
        self.connect(self.registration(token))
        self.pump(lambda: bool(self.reports))
        self.broker.identity = lambda w: ('opencode', 'ses_second', (1, 2))
        self.broker.drain()
        self.assertIsNone(self.broker.tokens[token][2])
        payload = self.registration(token)
        payload['session_id'] = 'ses_second'
        self.connect(payload)
        self.pump(lambda: self.broker.tokens[token][2] is not None)

    def test_cleanup(self):
        directory, path = self.broker.directory, self.broker.path
        broker = self.broker
        broker.close()
        self.assertFalse(broker.thread.is_alive())
        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(directory))
        self.assertEqual(self.removed, [42])
        self.assertEqual(broker.tokens, {})
        # Keep teardown simple without closing this instance twice.
        self.broker = Broker(self.boss, lambda *a: 43, self.removed.append)
