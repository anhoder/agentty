"""Native-free deterministic tests: python3 -m unittest kitty_tests.agent_history."""

import unittest

from kitty.agent_history import HistoryRead


class HistoryTests(unittest.TestCase):
    initial = ['6', '7', '8', '9', '10']

    def start(self, **kw):
        h = HistoryRead(self.initial, 80, 5, 0, passive_text='passive', **kw)
        self.assertEqual(h.step(self.initial, 80, 0.011).direction, 'down')
        self.assertEqual(h.step(self.initial, 80, 0.132).direction, 'up')
        return h

    def settled(self, h, rows, t):
        h.step(rows, 80, t)
        return h.step(rows, 80, t + 0.121)

    def restored(self, h, t=1):
        a = self.settled(h, self.initial, t)
        self.assertEqual(a.kind, 'done')
        self.assertEqual(a.result.restoration, 'restored')
        return a.result

    def test_multiple_pages_and_top(self):
        h = self.start()
        self.assertEqual(self.settled(h, ['3', '4', '5', '6', '7'], 0.14).direction, 'up')
        self.assertEqual(self.settled(h, ['0', '1', '2', '3', '4'], 0.27).direction, 'up')
        a = h.step(['0', '1', '2', '3', '4'], 80, 0.52)
        self.assertEqual((a.direction, a.amount), ('down', 9))
        r = self.restored(h)
        self.assertEqual(r.text, '\n'.join(map(str, range(11))))
        self.assertFalse(r.truncated)
        self.assertEqual(r.completeness, 'viewport_history')

    def test_already_top(self):
        h = self.start()
        self.assertEqual(h.step(self.initial, 80, 0.253).direction, 'down')
        self.assertEqual(self.restored(h).text, '\n'.join(self.initial))

    def test_enough_rows_stops_and_requires_verified_restore(self):
        h = self.start(requested_lines=7)
        a = self.settled(h, ['3', '4', '5', '6', '7'], 0.14)
        self.assertEqual((a.direction, a.amount), ('down', 3))
        self.assertEqual(h.reason, 'enough_lines')
        self.assertEqual(h.step(['3', '4', '5', '6', '7'], 80, 0.4).kind, 'wait')
        r = self.restored(h)
        self.assertEqual(r.text, '\n'.join(map(str, range(4, 11))))
        self.assertEqual(r.reason, 'enough_lines')
        self.assertEqual(r.completeness, 'viewport_history')
        self.assertTrue(r.truncated)

        h = self.start(requested_lines=7)
        self.settled(h, ['3', '4', '5', '6', '7'], 0.14)
        r = h.step(['3', '4', '5', '6', '7'], 80, 6).result
        self.assertEqual(r.text, 'passive')
        self.assertEqual(r.reason, 'restoration_failed')

    def test_non_bottom_probe(self):
        h = HistoryRead(self.initial, 80, 5, 0)
        h.step(self.initial, 80, 0.011)
        a = self.settled(h, ['9', '10', '11', '12', '13'], 0.02)
        self.assertEqual(a.direction, 'up')
        self.assertEqual(self.restored(h).reason, 'not_at_bottom')

    def test_unaligned_and_blank_single_anchor(self):
        for rows in (['a', 'b', 'c', 'd', '6'], ['', '', '', '', '']):
            h = self.start()
            self.assertEqual(self.settled(h, rows, 0.14).direction, 'down')
            self.assertEqual(self.restored(h).text, 'passive')

    def test_repeated_ambiguity(self):
        rows = ['x'] * 5
        h = HistoryRead(rows, 80, 5, 0)
        h.step(rows, 80, 0.011)
        h.step(rows, 80, 0.132)
        a = self.settled(h, ['y', 'x', 'x', 'x', 'x'], 0.14)
        self.assertEqual(a.direction, 'down')
        self.assertEqual(h.reason, 'ambiguous_overlap')

    def test_timeout_continuous_redraw_and_restore_bound(self):
        h = self.start()
        for t in (0.2, 1, 10, 14.99):
            self.assertEqual(h.step(self.initial, 80, t, redraw=True).kind, 'wait')
        a = h.step(self.initial, 80, 15, redraw=True)
        self.assertEqual(a.direction, 'down')
        a = h.step(['other'] * 5, 80, 20, redraw=True)
        self.assertEqual(a.result.reason, 'restoration_failed')

    def test_cancel(self):
        h = self.start()
        h.cancel(0.15)
        self.assertEqual(h.step(self.initial, 80, 0.15).direction, 'down')
        self.assertEqual(self.restored(h).reason, 'cancelled')

    def test_identity_and_guard_no_injection(self):
        for kwargs in ({'identity': 'new'}, {'valid': False}):
            h = self.start()
            a = h.step(self.initial, 80, 0.15, **kwargs)
            self.assertEqual(a.kind, 'done')
            self.assertEqual(a.result.restoration, 'failed')
            self.assertEqual(h.step(self.initial, 80, 1).kind, 'done')

    def test_resize_working_unknown(self):
        for cols, kwargs in ((81, {}), (80, {'working': True}), (80, {'unknown': True})):
            h = self.start()
            a = h.step(self.initial, cols, 0.15, **kwargs)
            self.assertEqual(a.direction, 'down')
            self.assertEqual(self.restored(h).text, 'passive')

    def test_limits(self):
        for kw, reason in (({'max_pages': 2}, 'page_limit'), ({'max_bytes': 12}, 'byte_limit')):
            h = self.start(**kw)
            a = self.settled(h, ['3', '4', '5', '6', '7'], 0.14)
            self.assertEqual(a.direction, 'down')
            self.assertEqual(self.restored(h).reason, reason)

    def test_static_chrome(self):
        initial = ['header', *self.initial, 'footer']
        h = HistoryRead(initial, 80, 7, 0, strip_header=1, strip_footer=1)
        h.step(initial, 80, 0.011)
        h.step(initial, 80, 0.132)
        older = ['header', '3', '4', '5', '6', '7', 'footer']
        h.step(older, 80, 0.14)
        self.assertEqual(h.step(older, 80, 0.261).direction, 'up')
        self.assertEqual(h.history, tuple(map(str, range(3, 11))))


if __name__ == '__main__':
    unittest.main()
