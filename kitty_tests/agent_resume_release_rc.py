"""Isolated policy tests; launch and kitty dependencies are stubbed."""

import importlib.util
import os
import shutil
import tempfile
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from . import requires_source_checkout


def load():
    launch = Mock(return_value=SimpleNamespace(id=99))
    parse = Mock(return_value=(SimpleNamespace(type='', cwd='', source_window='', copy_env=True, copy_cmdline=True, stdin_source='', watcher=[]), []))
    agents = ModuleType('kitty.agents')
    agents.detect_agent = lambda p: p[0] if p else None
    agents.session_ref_of_window = lambda w, a: w.session
    agents.resume_argv_for = Mock(return_value=('claude', '--resume', 's1'))
    manifest = ModuleType('kitty.agent_manifest')
    manifest.is_valid_session_id = lambda s: isinstance(s, str) and s.startswith('s')
    manifest.is_valid_session_path = lambda s: os.path.isabs(s)
    kitty_launch = ModuleType('kitty.launch')
    kitty_launch.launch = launch
    kitty_launch.parse_launch_args = parse
    with patch.dict('sys.modules', {'kitty.agents': agents, 'kitty.agent_manifest': manifest, 'kitty.launch': kitty_launch}):
        spec = importlib.util.spec_from_file_location('kitty.agent_launch', os.path.join(os.path.dirname(__file__), '..', 'kitty', 'agent_launch.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod, launch, parse


@requires_source_checkout
class Policy(unittest.TestCase):
    def setUp(self):
        self.mod, self.launch, self.parse = load()
        self.cwd = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cwd)
        self.boss = SimpleNamespace(active_tab='tab', active_window=None, window_id_map={})

    def test_start_registry_and_launch_shape(self):
        result = self.mod.start(self.boss, lambda k: {'agent': 'claude', 'cwd': self.cwd, 'request_id': 'r1'}.get(k))
        self.assertEqual(result['window_id'], 99)
        self.assertTrue(result['launch_requested'])
        opts, _ = self.parse.return_value
        self.assertEqual((opts.type, opts.cwd), ('window', self.cwd))
        self.assertEqual(self.launch.call_args.args[2], ['claude'])
        self.assertEqual(self.launch.call_args.kwargs, {'target_tab': 'tab', 'force_target_tab': True})
        self.assertFalse(opts.copy_env)
        self.assertFalse(opts.copy_cmdline)
        self.assertEqual(opts.stdin_source, 'none')
        self.assertEqual(opts.watcher, [])
        self.assertFalse(result['identity_confirmed'])

    def test_idempotency_and_bad_inputs_do_not_launch(self):
        def get(k):
            return {'agent': 'claude', 'cwd': self.cwd, 'request_id': 'r1'}.get(k)

        self.mod.start(self.boss, get)
        self.mod.start(self.boss, get)
        self.assertEqual(self.launch.call_count, 1)

    def test_invalid_resume_and_cwd_zero_launch(self):
        for payload in (
            {'agent': 'claude', 'session': 'bad', 'cwd': self.cwd},
            {'agent': 'claude', 'session': 's1', 'cwd': 'relative'},
            {'agent': 'claude', 'session': 's1', 'cwd': self.cwd, 'match': 'id:1'},
        ):
            with self.assertRaises(ValueError):
                self.mod.resume(self.boss, payload.get)
        self.launch.assert_not_called()

    def test_source_mode_and_other_duplicate(self):
        source = SimpleNamespace(
            id=1, child=SimpleNamespace(foreground_processes=['claude'], cwd=self.cwd), cwd_of_child=self.cwd, session='s1', tabref=lambda: 'source-tab'
        )
        self.boss.match_windows = Mock(return_value=[source])
        self.boss.window_id_map = {1: source}
        payload = {'match': 'id:1', 'expected_agent': 'claude', 'expected_session': 'stale'}
        with self.assertRaises(ValueError):
            self.mod.resume(self.boss, payload.get)
        self.launch.assert_not_called()
        payload['expected_session'] = 's1'
        self.mod.resume(self.boss, payload.get)
        self.assertEqual(self.launch.call_args.kwargs['target_tab'], 'source-tab')
        self.assertEqual(self.parse.return_value[0].source_window, 'id:1')
        with self.assertRaises(ValueError):
            self.mod.resume(self.boss, payload.get)
        self.assertEqual(self.launch.call_count, 1)

    def test_failure_not_cached_and_expiry(self):
        payload = {'agent': 'claude', 'cwd': self.cwd, 'request_id': 'retry'}
        self.launch.side_effect = RuntimeError('failed')
        with self.assertRaises(RuntimeError):
            self.mod.start(self.boss, payload.get)
        self.assertEqual(len(self.mod._requests), 0)
        self.launch.side_effect = None
        with patch.object(self.mod.time, 'monotonic', return_value=1):
            self.mod.start(self.boss, payload.get)
        with patch.object(self.mod.time, 'monotonic', return_value=2):
            self.assertTrue(self.mod.start(self.boss, payload.get)['replayed'])
            with self.assertRaises(ValueError):
                self.mod.start(self.boss, dict(payload, agent='pi').get)
        with patch.object(self.mod.time, 'monotonic', return_value=62):
            self.mod.start(self.boss, payload.get)
        self.assertEqual(self.launch.call_count, 3)

    def test_resume_manifest_and_duplicate(self):
        def get(k):
            return {'agent': 'claude', 'session': 's1', 'cwd': self.cwd}.get(k)

        self.mod.resume(self.boss, get)
        self.assertEqual(self.launch.call_args.args[2], ['claude', '--resume', 's1'])
        other = SimpleNamespace(child=SimpleNamespace(foreground_processes=['claude']), session='s1')
        self.boss.window_id_map = {1: other}
        with self.assertRaises(ValueError):
            self.mod.resume(self.boss, get)
        self.assertEqual(self.launch.call_count, 1)


if __name__ == '__main__':
    unittest.main()
