"""RC history safety checks, run using the kitty launcher test runner."""

from types import SimpleNamespace
from unittest.mock import Mock, patch
from unittest import TestCase

from kitty.rc.agent_read import HistoryWaiter, agent_read, mouse_guard, read_terminal
from kitty.rc.agent_wait import clear_waiters, pending
from kitty.fast_data_types import MOUSE_WHEEL_DOWN, MOUSE_WHEEL_UP, PRESS, send_mouse_event
from .base import BaseTest


class AgentHistoryRC(TestCase):
    ae = TestCase.assertEqual

    def test_rendered_limit_before_unwrap(self):
        window = SimpleNamespace(as_text=lambda **kw: 'old\nfirst\rsecond\nlast\n')
        result = read_terminal(window, 'recent-unwrapped', 2)
        self.ae(result['text'], 'second\nlast')
        self.assertTrue(result['completeness']['truncated'])
        self.ae(read_terminal(window, 'recent-unwrapped', 3)['text'], 'firstsecond\nlast')
        self.ae(read_terminal(window, 'recent', 3)['text'], 'first\nsecond\nlast')

    def test_verified_guard_includes_keyboard_flags(self):
        screen = SimpleNamespace(current_key_encoding_flags=lambda: 31)
        self.ae(mouse_guard(screen), (31, ''))

    def test_cancel_requests_recovery_not_close(self):
        waiter = object.__new__(HistoryWaiter)
        waiter.cancelled_at = None
        waiter.reader = Mock()
        waiter.cancel()
        waiter.cancel()
        self.ae(waiter.reader.cancel.call_count, 1)

    def test_help_discloses_input_authority(self):
        self.assertIn('mouse input', agent_read.desc)
        self.assertIn('--passive', agent_read.desc)
        self.assertIn('authorization', agent_read.desc)

    def make_waiter(self, sender=None):
        rows = ['6', '7', '8', '9', '10']
        screen = SimpleNamespace(columns=80, lines=5, scrolled_by=0,
            current_key_encoding_flags=lambda: 0, is_using_alternate_linebuf=lambda: True,
            linebuf=SimpleNamespace(line=lambda i: rows[i], is_continued=lambda i: False))
        window = SimpleNamespace(id=1, screen=screen,
            geometry=SimpleNamespace(left=0, right=800, top=0, bottom=100))
        boss = SimpleNamespace(window_id_map={1: window})
        responder = Mock()
        fallback = dict(source='recent', text='passive', completeness={'complete_transcript': False})
        identity = {'instance_id': 'a', 'agent': 'claude'}
        with patch('kitty.rc.agent_read.monotonic', return_value=0):
            waiter = HistoryWaiter(boss, 'history-test', responder, window, identity,
                                   fallback, 7, 0, sender=sender or Mock(return_value=True))
        pending(boss)[waiter.key] = waiter
        return waiter, rows, responder

    def tick(self, waiter, now, identity=None):
        with patch('kitty.rc.agent_read.monotonic', return_value=now), \
             patch('kitty.rc.agent_read.resolve_read_target', return_value=(waiter.window, identity or waiter.identity)), \
             patch('kitty.rc.agent_read.live_state', return_value='idle'):
            waiter.poll(0)

    def test_success_restores_original_viewport(self):
        import json
        w, rows, responder = self.make_waiter()
        self.tick(w, .02)
        self.tick(w, .15)
        rows[:] = ['3', '4', '5', '6', '7']
        self.tick(w, .17)
        self.tick(w, .30)
        rows[:] = ['6', '7', '8', '9', '10']
        self.tick(w, .32)
        self.tick(w, .35)
        self.assertTrue(w.closed)
        result = json.loads(responder.send_data.call_args.args[0])
        self.ae(result['restoration'], 'restored')
        self.ae(result['text'], '4\n5\n6\n7\n8\n9\n10')

    def test_native_false_is_passive(self):
        import json
        w, rows, responder = self.make_waiter(Mock(return_value=False))
        self.tick(w, .02)
        result = json.loads(responder.send_data.call_args.args[0])
        self.assertFalse(result['active_read'])
        self.ae(result['text'], 'passive')
        self.ae(result['completeness']['reason'], 'mouse_mode_unsupported')

    def test_identity_change_stops_before_input(self):
        w, rows, responder = self.make_waiter()
        self.tick(w, .02, {'instance_id': 'other'})
        w.sender.assert_not_called()
        self.assertTrue(w.closed)

    def test_shutdown_closes_without_recovery_input(self):
        w, rows, responder = self.make_waiter()
        self.tick(w, .02)
        count = w.sender.call_count
        clear_waiters(w.boss)
        self.tick(w, .15)
        self.ae(w.sender.call_count, count)
        self.assertFalse(pending(w.boss))

    def test_cancel_recovers_and_cleans_up(self):
        w, rows, responder = self.make_waiter()
        self.tick(w, .02)
        with patch('kitty.rc.agent_read.monotonic', return_value=.03):
            w.cancel()
        self.tick(w, .04)
        self.tick(w, .07)
        self.assertTrue(w.closed)
        self.assertFalse(pending(w.boss))


class NativeHistoryMouse(BaseTest):
    def test_native_wheel_signature_and_bytes(self):
        s = self.create_screen(cols=80, lines=24)
        self.assertFalse(send_mouse_event(s, 4, 5, 4, PRESS, 0, 45, 110))
        s.set_mode(1000, True)
        s.set_mode(1006, True)
        self.assertTrue(send_mouse_event(s, 4, 5, 4, PRESS, 0, 45, 110))
        self.assertTrue(send_mouse_event(s, 4, 5, 5, PRESS, 0, 45, 110))
        self.ae(s.callbacks.wtcbuf, b'\x1b[<64;5;6M\x1b[<65;5;6M')
