import unittest
from types import SimpleNamespace

from kitty_tests.test_agent_service_native_free import production_nodes

from . import requires_source_checkout


@requires_source_checkout
class PromptNativeFree(unittest.TestCase):
    def test_activity_gate_and_timeout_precedence(self):
        namespace = dict(Waiter=object, monotonic=lambda: 6)
        service = SimpleNamespace(
            binding=lambda w: {'instance_id': 'i'}, observe=lambda w: None, events=lambda *a: dict(events=[], cursor={}, resync_required=False)
        )
        namespace['service_for'] = lambda boss: service
        production_nodes('kitty/rc/agent_prompt.py', {'PromptWaiter'}, namespace)
        waiter = namespace['PromptWaiter']()
        waiter.boss = SimpleNamespace(window_id_map={1: SimpleNamespace(id=1)})
        waiter.spec = dict(window_id=1, result={'instance_id': 'i'}, cursor={}, active=False, activity_deadline=5, deadline=10, until=['idle'])
        results = []
        waiter.close = results.append
        waiter.poll(1)
        self.assertEqual(results[-1]['reason'], 'stalled')
        waiter.spec['deadline'] = 4
        waiter.poll(1)
        self.assertEqual(results[-1]['reason'], 'timeout')
