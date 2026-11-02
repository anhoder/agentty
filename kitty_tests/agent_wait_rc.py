"""Isolated RC tests: no native kitty imports or subprocesses."""
import importlib.util
import ast
from collections import deque
from dataclasses import asdict, dataclass
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class AgentWaitTest(unittest.TestCase):
    def setUp(self):
        self.removed = []
        self.sent = []
        self.callbacks = []
        agents = types.ModuleType('kitty.agents')
        agents.agent_event_cursor = lambda b: b.agent_tracker.event_seq
        agents.detect_agent = lambda p: p
        agents.session_id_of_window = lambda w, a: w.session
        agents.agent_identity = lambda w: (w.child.foreground_processes, w.session, w.child.pid)
        native = types.ModuleType('kitty.fast_data_types')
        native.add_timer = lambda cb, delay, repeat: self.callbacks.append(cb) or len(self.callbacks)
        native.remove_timer = self.removed.append
        base = types.ModuleType('kitty.rc.base')
        base.RemoteCommand = type('RemoteCommand', (), {})
        base.RemoteControlErrorWithoutTraceback = ValueError
        sent = self.sent
        base.AsyncResponder = lambda *a: types.SimpleNamespace(send_data=lambda data: sent.append(json.loads(data)))
        kt = types.ModuleType('kitty.types')
        kt.AsyncResponse = type('AsyncResponse', (), {})
        rc = types.ModuleType('kitty.remote_control')
        rc.active_async_requests = {}
        self.stub = patch.dict(sys.modules, {'kitty.agents': agents, 'kitty.fast_data_types': native,
                                            'kitty.rc.base': base, 'kitty.types': kt, 'kitty.remote_control': rc})
        self.stub.start()
        self.addCleanup(self.stub.stop)
        path = Path(__file__).resolve().parents[1] / 'kitty/rc/agent_wait.py'
        spec = importlib.util.spec_from_file_location('kitty.rc.agent_wait_isolated', path)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.tracker = types.SimpleNamespace(event_seq=4, event_batch=lambda *a: {'reason': 'timeout', 'next_seq': 4, 'events': []},
                                             snapshot_for=lambda wid: types.SimpleNamespace(generation=1), observe=lambda w: None)
        self.window = types.SimpleNamespace(child=types.SimpleNamespace(foreground_processes='codex', pid=42), session='s')
        self.boss = types.SimpleNamespace(agent_tracker=self.tracker, window_id_map={1: self.window})
        self.payload = {'match': 'id:1', 'expected_agent': 'codex', 'timeout_ms': 100, 'async_id': 'a'}

    def request(self):
        return self.mod.agent_wait.response_from_kitty(self.boss, None, lambda key, missing=None: self.payload.get(key, missing))

    def test_validation(self):
        for key, value in (('match', 'all'), ('match', 'id:-1'), ('expected_agent', ''),
                           ('timeout_ms', 10001), ('after_seq', -2), ('after_seq', None), ('max_events', 0), ('after_seq', True)):
            with self.subTest(key=key, value=value), patch.dict(self.payload, {key: value}):
                with self.assertRaises(ValueError):
                    self.request()

    def test_timeout_and_idempotent_cleanup(self):
        self.request()
        waiter = self.mod.pending(self.boss)['a']
        self.callbacks[0](1)
        waiter.close()
        self.assertEqual(self.sent[0]['reason'], 'timeout')
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(self.mod.pending(self.boss))

    def test_identity_change(self):
        self.request()
        self.window.child.pid = 43
        self.mod.wake_waiters(self.boss, 1)
        self.assertEqual(self.sent[0]['reason'], 'identity_changed')
        self.assertEqual(self.removed, [1, 2])

    def test_closed(self):
        self.request()
        self.boss.window_id_map.clear()
        self.mod.wake_waiters(self.boss, 1)
        self.assertEqual(self.sent[0]['reason'], 'window_closed')

    def test_closed_precedes_buffer_access(self):
        self.boss.window_id_map.clear()
        self.tracker.event_batch = lambda *a: self.fail('closed window read the buffer')
        self.assertEqual(json.loads(self.request())['reason'], 'window_closed')

    def production_tracker(self):
        path = Path(__file__).resolve().parents[1] / 'kitty/agents.py'
        tree = ast.parse(path.read_text())
        nodes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in {'AgentSnapshot', 'AgentTracker'}]
        namespace = {'Any': object, 'AgentState': str, 'dataclass': dataclass, 'asdict': asdict,
                     'deque': deque, 'Sequence': list}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
        self.boss.agent_tracker = self.tracker = namespace['AgentTracker']()
        self.tracker.observe = lambda w: None
        self.tracker.generations[1] = 1
        self.tracker.identities[1] = ('codex', 's', 42)
        return namespace['AgentSnapshot']

    def test_production_buffer_cross_request_working(self):
        snapshot = self.production_tracker()
        working = snapshot(1, 'codex', 'working', '', '', generation=1)
        self.tracker.snapshots[1] = working
        self.tracker.record_change(1, None, working)
        self.payload.update(until='done', require_working=True, after_seq=0, timeout_ms=0)
        first = json.loads(self.request())
        self.assertEqual(first['working_seq'], 1)
        self.assertEqual(first['working_generation'], 1)
        done = snapshot(1, 'codex', 'done', '', '', generation=1)
        self.tracker.snapshots[1] = done
        self.tracker.record_change(1, working, done)
        self.payload.update(after_seq=first['next_seq'], working_seq=1, working_generation=1)
        self.assertEqual(json.loads(self.request())['reason'], 'changed')
        self.payload['working_generation'] = 2
        with self.assertRaises(ValueError):
            self.request()

    def test_production_tombstone_cleanup(self):
        snapshot = self.production_tracker()
        self.tracker.snapshots[1] = snapshot(1, 'codex', 'working', '', '', generation=1)
        self.tracker.discard(1)
        event = self.tracker.event_batch(1, 0, 100)['events'][0]
        self.assertEqual(event['reason'], 'window_closed')
        self.assertEqual(event['generation'], 1)
        self.assertEqual(event['identity'], ('codex', 's', 42))
        self.assertFalse(self.tracker.identities)
        self.assertFalse(self.tracker.generations)

    def test_production_handler_async_lifecycle(self):
        path = Path(__file__).resolve().parents[1] / 'kitty/remote_control.py'
        tree = ast.parse(path.read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'handle_cmd')
        active = {}
        async_response = self.mod.AsyncResponse
        no_response = type('NoResponse', (), {})
        command = types.SimpleNamespace(reads_streaming_data=False, cancel_async_request=lambda *a: None)
        namespace = dict(Any=object, BossType=object, WindowType=object, AsyncResponse=async_response,
                         NoResponse=no_response, version=(1, 0), active_async_requests=active,
                         active_streams={}, monotonic=lambda: 1, command_for_name=lambda n: command,
                         PayloadGetter=lambda c, p: p)
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
        handle = namespace['handle_cmd']
        cmd = {'version': (1, 0), 'cmd': 'agent-wait', 'async': 'a'}
        for answer in ('result', no_response(), None, async_response()):
            command.response_from_kitty = lambda *a: answer
            handle(self.boss, None, cmd.copy(), 0, None)
            self.assertEqual('a' in active, isinstance(answer, async_response))
        handle(self.boss, None, dict(cmd, cancel_async=True), 0, None)
        self.assertFalse(active)
        def fail(*a):
            raise ValueError('failed')
        command.response_from_kitty = fail
        with self.assertRaises(ValueError):
            handle(self.boss, None, cmd.copy(), 0, None)
        self.assertFalse(active)

    def test_cancel_and_shutdown(self):
        self.request()
        self.mod.agent_wait.cancel_async_request(self.boss, None, lambda key, missing=None: self.payload.get(key, missing))
        self.mod.clear_waiters(self.boss)
        self.assertEqual(self.removed, [1, 2])
        self.assertFalse(self.sent)

    def test_truncated_is_immediate(self):
        self.tracker.event_batch = lambda *a: {'reason': 'truncated', 'next_seq': 4, 'events': [], 'resync_required': True}
        self.assertTrue(json.loads(self.request())['resync_required'])
        self.assertFalse(self.callbacks)

    def test_cursors(self):
        for value, expected in ((-1, 4), (0, 0), (2, 2)):
            self.payload['after_seq'] = value
            self.request()
            self.assertEqual(self.mod.pending(self.boss)['a'].spec['after_seq'], expected)
            self.mod.clear_waiters(self.boss)

    def test_reply_failure_isolated(self):
        self.request()
        waiter = self.mod.pending(self.boss)['a']
        waiter.responder.send_data = lambda data: (_ for _ in ()).throw(RuntimeError('disconnected'))
        waiter.close({'reason': 'timeout'})
        waiter.close()
        self.assertFalse(self.mod.pending(self.boss))
        self.assertEqual(self.boss._agent_observe_timer, 0)

    def test_working_before_done(self):
        self.payload.update(until='done', require_working=True, after_seq=0)
        events = [{'seq': 1, 'snapshot': {'state': 'done', 'generation': 1}}]
        self.tracker.event_batch = lambda *a: {'reason': 'changed', 'events': list(events), 'next_seq': events[-1]['seq']}
        self.request()
        self.assertFalse(self.sent)
        events[:] = [{'seq': 2, 'snapshot': {'state': 'working', 'generation': 1}},
                     {'seq': 3, 'snapshot': {'state': 'done', 'generation': 1}}]
        self.mod.wake_waiters(self.boss, 1)
        self.assertEqual(self.sent[0]['reason'], 'changed')


if __name__ == '__main__':
    unittest.main()
