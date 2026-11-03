import unittest
from types import SimpleNamespace
from unittest.mock import patch

from kitty.rc.agent_prompt import PromptWaiter, agent_prompt


class AgentPromptRC(unittest.TestCase):
    def test_contract(self):
        self.assertEqual(agent_prompt.response_timeout, 310)
        self.assertIn('until/str', agent_prompt.protocol_spec)

    def run_wait(self, events=(), now=1, deadline=10, activity=5, present=True):
        window = SimpleNamespace(id=1)
        boss = SimpleNamespace(window_id_map={1: window} if present else {})
        service = SimpleNamespace(binding=lambda w: {'instance_id': 'i', 'identity': ('a', 's')},
                                  observe=lambda w: None,
                                  events=lambda *a: dict(events=list(events), cursor={}, resync_required=False))
        waiter = PromptWaiter(boss, 'key', dict(window_id=1, cursor={}, result={'instance_id': 'i'},
                             active=False, deadline=deadline, activity_deadline=activity,
                             until=['idle', 'done', 'blocked']), None)
        results = []
        waiter.close = results.append
        with patch('kitty.rc.agent_prompt.service_for', return_value=service), patch('kitty.rc.agent_prompt.monotonic', return_value=now):
            waiter.poll(1)
        return results

    def test_startup_idle_not_completion(self):
        self.assertEqual(self.run_wait([dict(seq=1, type='state', data={'state': 'idle'})]), [])

    def test_fast_provider_activity(self):
        events = [dict(seq=i, type='provider_event', data={'event': {'kind': 'session.status',
                  'session_id': 's', 'payload': {'status': state}}})
                  for i, state in enumerate(('busy', 'idle', 'busy'), 1)]
        result = self.run_wait(events)[0]
        self.assertEqual(result['reason'], 'settled')
        self.assertEqual(result['working_seq'], 1)
        self.assertEqual(result['completion_seq'], 2)

    def test_deadlines_and_close(self):
        self.assertEqual(self.run_wait(now=6)[0]['reason'], 'stalled')
        self.assertEqual(self.run_wait(now=6, deadline=4)[0]['reason'], 'timeout')
        self.assertEqual(self.run_wait(present=False)[0]['reason'], 'window_closed')
