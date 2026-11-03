"""Exercise the actual pure terminal reader without importing native kitty modules."""

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

tree = ast.parse((Path(__file__).resolve().parents[1] / 'kitty/rc/agent_read.py').read_text())
namespace = {}
exec(
    compile(
        ast.Module(
            body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'read_terminal'],
            type_ignores=[],
        ),
        '<agent_read>',
        'exec',
    ),
    namespace,
)
read_terminal = namespace['read_terminal']


class AgentReadTest(unittest.TestCase):
    def test_history_wraps_and_completeness(self):
        calls = []
        window = SimpleNamespace(as_text=lambda **kw: calls.append(kw) or 'past\na\rb\nend\n')
        result = read_terminal(window, 'recent', 2)
        self.assertEqual(result['text'], 'b\nend')
        self.assertTrue(result['completeness']['truncated'])
        self.assertFalse(result['completeness']['complete_transcript'])
        self.assertEqual(read_terminal(window, 'recent-unwrapped', 2)['text'], 'b\nend')
        self.assertEqual(calls, [dict(add_history=True, add_wrap_markers=True)] * 2)

    def test_full_screens_and_default_limit(self):
        window = SimpleNamespace(
            as_text=lambda **kw: '\n'.join(map(str, range(100))), agent_detection_snapshot=lambda: SimpleNamespace(lines=('live', '', 'end'))
        )
        self.assertEqual(len(read_terminal(window, 'recent', None)['text'].split('\n')), 80)
        self.assertEqual(read_terminal(window, 'detection', None)['text'], 'live\n\nend')
        self.assertEqual(len(read_terminal(window, 'visible', None)['text'].split('\n')), 100)
        for source, lines in [('invalid', None), ('visible', 1), ('recent', True), ('recent', 0), ('recent', 10001)]:
            with self.assertRaises(ValueError):
                read_terminal(window, source, lines)
