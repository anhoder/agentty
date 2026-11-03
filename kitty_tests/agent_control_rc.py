"""Isolated RC contract tests; no native extension, terminal or subprocess needed.

Dependencies are stubbed only while loading the command and sys.modules is
restored immediately. This does not validate native key encoding or arbitration.
Run with: python3 -m unittest kitty_tests.agent_control_rc
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch


def load_command():
    class RemoteCommand:
        Args = staticmethod(lambda **kw: SimpleNamespace(**kw))

    def module(name, **attrs):
        ans = ModuleType(name)
        ans.__dict__.update(attrs)
        return ans

    def parse(key):
        if key == 'bad+key':
            raise ValueError('bad key')
        return SimpleNamespace(is_native=key.startswith('0x'), key=0 if key == 'invalid' else 1)

    base = dict.fromkeys(('ArgsType', 'Boss', 'PayloadGetType', 'PayloadType', 'RCOptions', 'ResponseType', 'Window'), object)
    base.update(RemoteCommand=RemoteCommand, RemoteControlErrorWithoutTraceback=ValueError)
    class StateAuthority:
        full_lifecycle = 'full_lifecycle'

    def authority_for(agent):
        return StateAuthority.full_lifecycle

    deps = {
            'kitty.rc.base': module('kitty.rc.base', **base),
            'kitty.agents': module('kitty.agents', AgentState=SimpleNamespace(idle='idle', done='done'),
                               agent_event_cursor=lambda boss: boss.cursor,
                               agent_identity=lambda w: ('claude', w.session, 1234),
                               detect_agent=lambda p: p[0]['agent'] if p else None,
                               session_ref_of_window=lambda w, a: w.session,
                               snapshot_for_window=lambda w, s: (SimpleNamespace(agent=w.child.foreground_processes[0]['agent'],
                                                                               state=w.state), False)),
         'kitty.agent_manifest': module('kitty.agent_manifest', authority_for=authority_for,
                                        StateAuthority=StateAuthority),
         'kitty.options.utils': module('kitty.options.utils', parse_shortcut=parse),
        'kitty.utils': module('kitty.utils', sanitize_for_bracketed_paste=lambda b: b),
    }
    spec = importlib.util.spec_from_file_location('kitty.rc.agent_control', Path(__file__).resolve().parents[1] / 'kitty/rc/agent_control.py')
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, deps):
        spec.loader.exec_module(loaded)
    return loaded


class FakeWindow:
    id = 42
    session = 'live-session'
    state = 'idle'

    def __init__(self):
        self.child = SimpleNamespace(foreground_processes=[{'agent': 'claude'}])
        self.screen = SimpleNamespace(in_bracketed_paste_mode=False)
        self.program_status = SimpleNamespace(as_list=lambda: [{'state': 'idle'}])
        self.events = []

    def agent_detection_snapshot(self):
        return object()

    def write_to_child(self, data):
        self.events.append(('write', data))

    def send_key(self, *keys):
        self.events.append(('keys', keys))


class AgentControlRC(unittest.TestCase):
    def setUp(self):
        self.mod = load_command()
        self.window = FakeWindow()
        self.boss = SimpleNamespace(match_windows=Mock(return_value=[self.window]), set_active_window=Mock(), cursor=17,
                                    agent_tracker=SimpleNamespace(observe=Mock(return_value=SimpleNamespace(generation=3)),
                                                                  snapshot_for=lambda wid: None))
        self.payload = dict(action='prompt', arguments=['hello'], match='id:42', expected_agent='claude')

    def run_command(self, **changes):
        self.payload.update(changes)
        return json.loads(self.mod.agent_control.response_from_kitty(self.boss, None, self.payload.get))

    def rejected(self, **changes):
        with self.assertRaises(ValueError):
            self.run_command(**changes)
        self.assertEqual(self.window.events, [])
        self.boss.set_active_window.assert_not_called()

    def test_selectors(self):
        for selector in (None, '', 'all', 'id:0', 'id:-1', 'id:42 or id:43', 'id:042', 'id:42\n'):
            with self.subTest(selector=selector):
                self.rejected(match=selector)
        for windows in ([], [self.window, self.window], [SimpleNamespace(id=43)]):
            self.boss.match_windows.return_value = windows
            self.rejected(match='id:42')

    def test_live_identity_and_session(self):
        self.rejected(expected_agent=None)
        self.rejected(expected_agent='codex')
        self.rejected(expected_agent='claude', expected_session='stale')
        self.rejected(expected_session='')
        self.payload.pop('expected_session')
        self.window.child.foreground_processes = []
        self.rejected()

    def test_live_states(self):
        for state in ('blocked', 'working', 'unknown'):
            self.window.state = state
            self.rejected()

    def test_literal_write_then_enter(self):
        result = self.run_command(arguments=[r'hello\n\t\u1234'], expected_session='live-session')
        self.assertTrue(result['input_requested'])
        self.assertEqual(result['baseline_seq'], 17)
        self.assertEqual(result['identity']['generation'], 3)
        self.boss.agent_tracker.observe.assert_called_once_with(self.window)
        self.assertFalse(result['cancel_requested'])
        self.assertEqual(self.window.events, [('write', br'hello\n\t\u1234'), ('keys', ('enter',))])

    def test_controls_and_utf8(self):
        for text in ('a\x1bb', '\r', '\x00', '\x7f', '\x85', '\ud800', ''):
            self.rejected(arguments=[text])

    def test_bracketed_paste(self):
        for text in ('one\ntwo', 'one\ttwo'):
            self.rejected(arguments=[text])
        self.window.screen.in_bracketed_paste_mode = True
        with patch.object(self.mod, 'sanitize_for_bracketed_paste', wraps=lambda b: b) as sanitize:
            self.run_command(arguments=['one\n\ttwo'])
            sanitize.assert_called_once_with(b'one\n\ttwo')
        self.assertEqual(self.window.events, [('write', b'\x1b[200~one\n\ttwo\x1b[201~'), ('keys', ('enter',))])

    def test_keys_validate_all_before_sending(self):
        for keys in ([], ['enter', 'bad+key'], ['enter', '0x12'], ['invalid'], [1]):
            self.rejected(action='send-keys', arguments=keys)
        result = self.run_command(action='send-keys', arguments=['ctrl+l', 'enter'])
        self.assertTrue(result['input_requested'])
        self.assertEqual(result['baseline_seq'], 17)
        self.assertIsNone(result['identity']['generation'])
        self.boss.agent_tracker.observe.assert_not_called()
        self.assertEqual(self.window.events, [('keys', ('ctrl+l', 'enter'))])

    def test_focus_cancel(self):
        self.rejected(action='focus', arguments=['extra'])
        self.assertTrue(self.run_command(action='focus', arguments=[])['focus_requested'])
        self.boss.set_active_window.assert_called_once_with(self.window, switch_os_window_if_needed=True)
        self.assertTrue(self.run_command(action='cancel', arguments=[])['cancel_requested'])
        self.assertEqual(self.window.events, [('keys', ('ctrl+c',))])

    def test_payload_shapes(self):
        self.rejected(action='invalid')
        self.rejected(arguments='text')
        self.rejected(arguments=['one', 'two'])

    def test_first_rest_contract(self):
        args = self.mod.agent_control.args
        self.assertEqual(args.first_rest, ('action', 'arguments'))
        self.assertEqual(args.json_field, 'action')
        self.assertEqual(args.minimum_count, 1)
        opts = SimpleNamespace(match='id:42', expected_agent='claude', expected_session='')
        result = self.mod.agent_control.message_to_kitty(None, opts, ['prompt', 'literal\\n'])
        self.assertEqual(result['arguments'], ['literal\\n'])


if __name__ == '__main__':
    unittest.main()
