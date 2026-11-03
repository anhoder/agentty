import ast
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from kitty.agent_service import AgentService


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.identity = ('agent', 'session', 10)
        stub = types.ModuleType('kitty.agents')
        stub.agent_identity = lambda window: self.identity
        self.patch = patch.dict(sys.modules, {'kitty.agents': stub})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.window = types.SimpleNamespace(id=1)
        self.service = AgentService(object(), max_events=2, max_bytes=1024)

    def test_binding(self):
        first = self.service.inspect(self.window)
        self.assertEqual(first, self.service.inspect(self.window))
        self.identity = ('agent', 'session', 11)
        second = self.service.inspect(self.window)
        self.assertNotEqual(first['instance_id'], second['instance_id'])
        self.assertEqual(second['binding_revision'], 2)
        with self.assertRaises(ValueError):
            self.service.guard(self.window, {'expected_instance': first['instance_id']}.get)

    def test_target_alone(self):
        stub = sys.modules['kitty.agents']
        stub.detect_agent = lambda processes: 'agent'
        stub.session_ref_of_window = lambda window, agent: 'session'
        self.window.child = types.SimpleNamespace(foreground_processes=[])
        self.service.boss = types.SimpleNamespace(all_windows=[self.window])
        target = self.service.binding(self.window)['instance_id']
        self.assertIs(self.service.resolve_target({'target': target}.get, require_expected=True), self.window)
        with self.assertRaises(ValueError):
            self.service.resolve_target({'target': target, 'expected_agent': 'other'}.get)

    def test_bounded_epoch_journal(self):
        cursor = self.service.cursor()
        for i in range(5):
            self.service.emit(1, 'test', {'i': i})
        self.assertEqual(len(self.service.journal), 2)
        self.assertTrue(self.service.events(cursor)['resync_required'])
        self.assertTrue(self.service.events({'runtime_epoch': 'old', 'seq': 5})['resync_required'])
        self.service.emit(1, 'big', {'text': 'x' * 2048})
        self.assertLessEqual(self.service.event_bytes, 1024)

    def test_submission_replay_without_provider_dispatch(self):
        self.service.report(self.window, {'type': 'register', 'capabilities': ['cancel']})
        result = self.service.remember('req', 'fingerprint', {'confirmation': 'unavailable'})
        self.assertEqual(self.service.replay('req', 'fingerprint'), result)
        with self.assertRaises(ValueError):
            self.service.replay('req', 'other')
        with self.assertRaises(ValueError):
            self.service.report(self.window, {'type': 'ack', 'status': 'completed'})

    def test_credentials_and_revocation(self):
        self.service.report(self.window, {'type': 'register', 'token': 'SECRET', 'capabilities': ['cancel']})
        self.assertNotIn('SECRET', repr(self.service.journal))
        self.service.report(self.window, {'type': 'event', 'event': {'kind': 'session.created', 'token': 'SECRET', 'payload': {'token': 'SECRET'}}})
        self.assertNotIn('SECRET', repr(self.service.journal))
        self.service.report(self.window, {'type': 'gap'})
        self.assertEqual(self.service.inspect(self.window)['capabilities']['provider'], [])
        with self.assertRaises(ValueError):
            self.service.report(self.window, {'type': 'event', 'event': {}})

    def test_cancel_needs_evidence(self):
        self.assertFalse(hasattr(self.service, 'operation'))
        with self.assertRaises(ValueError):
            self.service.report(self.window, {'type': 'ack', 'status': 'completed'})

    def test_birth_identity_and_event_negative_cases(self):
        self.window.child = types.SimpleNamespace(foreground_processes=[{'pid': 10, 'start_time': 1}])
        first = self.service.binding(self.window)
        self.window.child.foreground_processes[0]['start_time'] = 2
        self.assertNotEqual(first['instance_id'], self.service.binding(self.window)['instance_id'])
        self.service.report(self.window, {'type': 'register'})
        for event in (
            {},
            {'kind': []},
            {'kind': 'child', 'session_id': 1},
            {'kind': 'child', 'session_id': 'x', 'parent_session_id': 'x'},
            {'kind': 'interaction', 'interaction_id': {}},
        ):
            with self.subTest(event=event), self.assertRaises(ValueError):
                self.service.report(self.window, {'type': 'event', 'event': event})

    def test_dispatch_limit(self):
        for i in range(256):
            self.service.remember(str(i), str(i), {})
        self.service.remember('overflow', '', {})
        self.assertEqual(len(self.service.requests), 256)
        self.assertIsNone(self.service.replay('0', '0'))

    def test_provider_status_children_interactions_and_clones(self):
        self.service.report(self.window, {'type': 'register', 'capabilities': ['session_status']})

        def event(kind, **fields):
            return self.service.report(self.window, {'type': 'event', 'event': dict(kind=kind, **fields)})

        result = event('session.status', session_id='root', payload={'status': 'busy', 'secret': 'DROP'})
        self.assertEqual(self.service.journal[-1][0]['data']['event']['payload'], {'status': 'busy'})
        result['provider']['capabilities'].append('cancel')
        self.assertNotIn('cancel', self.service.providers[1]['capabilities'])
        for status in ('working', {}, None):
            with self.assertRaises(ValueError):
                event('session.status', payload={'status': status})
        event('session.created', session_id='child', parent_session_id='root')
        event('session.created', session_id='grandchild', parent_session_id='child')
        event('question.asked', session_id='child', interaction_id='q')
        self.assertIn('q', self.service.providers[1]['interactions'])
        event('question.rejected', session_id='child', interaction_id='q')
        self.assertNotIn('q', self.service.providers[1]['interactions'])
        event('permission.asked', session_id='grandchild', interaction_id='p')
        event('session.deleted', session_id='root')
        self.assertEqual(self.service.providers[1]['children'], {})
        self.assertEqual(self.service.providers[1]['interactions'], {})
        self.service.remember('key', 'fp', {'status': 'input_requested'})
        replay = self.service.replay('key', 'fp')
        self.assertEqual(replay['status'], 'input_requested')
        replay['status'] = 'corrupt'
        self.assertEqual(self.service.replay('key', 'fp')['status'], 'input_requested')
        event('gap', payload={'reason': 'reconnect', 'partial': True, 'children': 'unknown'})
        self.assertTrue(self.service.providers[1]['connected'])
        self.assertEqual(self.service.providers[1]['completeness']['children'], 'unknown')
        event('session.status', session_id='session', payload={'status': 'idle', 'snapshot': True, 'partial': True})
        payload = self.service.journal[-1][0]['data']['event']['payload']
        self.assertEqual(payload, {'status': 'idle', 'snapshot': True, 'partial': True})


def production_nodes(path, names, namespace):
    """Execute actual RC lifecycle classes with native dependencies replaced."""
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / path).read_text())
    selected = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names]
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *selected], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), path, 'exec'), namespace)


class SubscriberTest(unittest.TestCase):
    def setUp(self):
        self.timers = {}
        self.active = {}

        def add_timer(callback, delay, repeat):
            key = len(self.timers) + 1
            self.timers[key] = callback
            return key

        remote = types.ModuleType('kitty.remote_control')
        remote.active_async_requests = self.active
        native = types.ModuleType('kitty.fast_data_types')
        native.remove_timer = lambda key: self.timers.pop(key, None)
        agents = types.ModuleType('kitty.agents')
        agents.snapshot_for_window = lambda *args: (None, False)
        self.patch = patch.dict(sys.modules, {'kitty.remote_control': remote, 'kitty.fast_data_types': native, 'kitty.agents': agents})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.boss = types.SimpleNamespace(all_windows=[])
        self.service = AgentService(self.boss)
        self.ns = dict(Any=object, json=json, add_timer=add_timer, remove_timer=native.remove_timer, service_for=lambda boss: self.service)
        production_nodes('kitty/rc/agent_wait.py', {'pending', 'Waiter', 'clear_waiters'}, self.ns)
        production_nodes('kitty/rc/agent_subscribe.py', {'JournalWaiter'}, self.ns)
        self.sent = []

    def waiter(self):
        waiter = self.ns['JournalWaiter'](
            self.boss, 'request', {'window_id': None, 'cursor': self.service.cursor(), 'maximum': 100}, types.SimpleNamespace(send_data=self.sent.append)
        )
        self.ns['pending'](self.boss)['request'] = waiter
        self.active['request'] = object()
        waiter.timer = self.ns['add_timer'](waiter.timeout, 1, False)
        waiter.poll_timer = self.ns['add_timer'](waiter.poll, 0.05, True)
        return waiter

    def test_timeout_cleanup(self):
        waiter = self.waiter()
        # Timer backend removes a one-shot timer before invoking it.
        self.timers.pop(waiter.timer)
        waiter.timeout(waiter.timer)
        self.assertEqual(json.loads(self.sent[0])['reason'], 'timeout')
        self.assertFalse(self.timers)
        self.assertFalse(self.active)
        self.assertFalse(self.ns['pending'](self.boss))

    def test_disconnect_cleanup(self):
        waiter = self.waiter()
        waiter.close()
        waiter.close()
        self.assertEqual(self.sent, [])
        self.assertFalse(self.timers)
        self.assertFalse(self.active)

    def test_epoch_wakes(self):
        waiter = self.waiter()
        self.service.runtime_epoch = 'new'
        waiter.poll(0)
        self.assertTrue(json.loads(self.sent[0])['resync_required'])
        self.assertFalse(self.timers)

    def test_window_closed_event(self):
        stub = types.ModuleType('kitty.agents')
        stub.snapshot_for_window = lambda *args: (None, False)
        self.service.bindings[5] = dict(instance_id='old', identity=(), binding_revision=1)
        waiter = self.waiter()
        with patch.dict(sys.modules, {'kitty.agents': stub}):
            waiter.poll(0)
        self.assertEqual(json.loads(self.sent[0])['events'][0]['type'], 'removed')
        self.assertFalse(self.timers)

    def test_waiter_queue_limit(self):
        # Execute the production subscribe handler, not a duplicated limit check.
        class Events:
            protocol_spec = options_spec = ''

            def response_from_kitty(inner, *args):
                return json.dumps(self.service.events())

        self.ns.update(AgentEvents=Events, RemoteControlErrorWithoutTraceback=ValueError, AsyncResponder=lambda *args: None, AsyncResponse=lambda: None)
        production_nodes('kitty/rc/agent_subscribe.py', {'AgentSubscribe'}, self.ns)
        self.ns['pending'](self.boss).update({str(i): object() for i in range(16)})
        payload = {'timeout_ms': 100, 'async_id': 'new'}
        with self.assertRaises(ValueError):
            self.ns['AgentSubscribe']().response_from_kitty(self.boss, None, lambda key, missing=None: payload.get(key, missing))

    def test_production_cancel_handler(self):
        class Events:
            protocol_spec = options_spec = ''

        self.ns.update(AgentEvents=Events)
        production_nodes('kitty/rc/agent_subscribe.py', {'AgentSubscribe'}, self.ns)
        self.waiter()
        self.ns['AgentSubscribe']().cancel_async_request(self.boss, None, lambda key, missing=None: 'request')
        self.assertFalse(self.timers)
        self.assertFalse(self.active)
        self.assertFalse(self.sent)

    def test_poll_state_source(self):
        window = types.SimpleNamespace(id=8, agent_detection_snapshot=lambda: None)
        stub = sys.modules['kitty.agents']
        stub.agent_identity = lambda window: ('agent', 'session', 42)
        stub.snapshot_for_window = lambda *args: (types.SimpleNamespace(state=types.SimpleNamespace(value='working')), False)
        self.boss.all_windows = [window]
        waiter = self.waiter()
        waiter.poll(0)
        result = json.loads(self.sent[0])
        state = next(e for e in result['events'] if e['type'] == 'state')
        self.assertEqual(state['data']['state_source'], 'live_observation')
        self.assertEqual(state['data']['state'], 'working')


if __name__ == '__main__':
    unittest.main()
