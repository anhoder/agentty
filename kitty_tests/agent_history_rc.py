"""RC history safety checks, run using the kitty launcher test runner."""
from types import SimpleNamespace
from unittest.mock import Mock
from unittest import TestCase

from kitty.rc.agent_read import HistoryWaiter, agent_read, mouse_guard, read_terminal

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
