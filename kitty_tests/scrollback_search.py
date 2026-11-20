#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, kitty contributors

import json
from types import SimpleNamespace
from unittest.mock import patch

from .base import BaseTest, parse_bytes


class TestScrollbackSearch(BaseTest):
    def test_search_unicode_wrapping_and_individual_matches(self):
        from kitty.scrollback_search import find_matches

        rows = (
            ('a界e\u0301foo', (0, 1, 3, 3, 4, 5, 6, 7), False),
            ('bar foo', tuple(range(8)), True),
            ('bar', tuple(range(4)), False),
        )
        self.ae(find_matches(rows, -2, '界'), ((-2, 1, -2, 3),))
        self.ae(find_matches(rows, -2, 'e'), ((-2, 3, -2, 4),))
        self.ae(find_matches(rows, -2, '\u0301'), ((-2, 3, -2, 4),))
        self.ae(find_matches(rows, -2, 'FOOBAR'), ((-2, 4, -1, 3),))
        self.ae(find_matches(rows, -2, 'foo'), ((-2, 4, -2, 7), (-1, 4, -1, 7)))
        self.ae(find_matches(rows, -2, 'foobarbar'), ())
        self.ae(find_matches(rows, -2, ''), ())

    def test_search_keeps_unicode_offsets_when_case_changes_length(self):
        from kitty.scrollback_search import find_matches

        rows = (('İXİx', tuple(range(5)), False),)
        self.ae(find_matches(rows, 0, 'x'), ((0, 1, 0, 2), (0, 3, 0, 4)))
        self.ae(find_matches((('a.b aab', tuple(range(8)), False),), 0, 'a.b'), ((0, 0, 0, 3),))

    def test_search_case_and_regex_options(self):
        from kitty.scrollback_search import bounded_regex_matches, find_matches

        rows = (('Needle needle a.b axb 界', tuple(range(24)), False),)
        self.ae(find_matches(rows, 0, 'Needle', True), ((0, 0, 0, 6),))
        self.ae(find_matches(rows, 0, 'a.b'), ((0, 14, 0, 17),))
        self.ae(find_matches(rows, 0, r'a.b', regex=True), ((0, 14, 0, 17), (0, 18, 0, 21)))
        self.ae(bounded_regex_matches(rows, 0, r'(?<= )needle|界', True), ((0, 7, 0, 13), (0, 22, 0, 23)))
        self.ae(find_matches(rows, 0, '^|$', regex=True), ())

    def test_search_whole_words_with_case_regex_and_soft_wrap(self):
        from kitty.scrollback_search import bounded_regex_matches, find_matches

        text = 'cat scatter cat_cat CAT dog dogma café caféine'
        rows = ((text, tuple(range(len(text) + 1)), False),)
        self.ae(find_matches(rows, 0, 'cat', whole_word=True), ((0, 0, 0, 3), (0, 20, 0, 23)))
        self.ae(find_matches(rows, 0, 'cat', case_sensitive=True, whole_word=True), ((0, 0, 0, 3),))
        self.ae(find_matches(rows, 0, 'cat|dog', regex=True, whole_word=True), ((0, 0, 0, 3), (0, 20, 0, 23), (0, 24, 0, 27)))
        self.ae(bounded_regex_matches(rows, 0, '(?i)CAT|DOG', True, True), ((0, 0, 0, 3), (0, 20, 0, 23), (0, 24, 0, 27)))
        self.ae(find_matches(rows, 0, 'café', whole_word=True), ((0, 34, 0, 38),))
        wrapped = (('a cat', tuple(range(6)), False), ('fish cat', tuple(range(9)), True))
        self.ae(find_matches(wrapped, -1, 'cat', whole_word=True), ((0, 5, 0, 8),))
        self.ae(find_matches(rows, 0, '(?x) cat | dog # comment', regex=True, whole_word=True), ((0, 0, 0, 3), (0, 20, 0, 23), (0, 24, 0, 27)))

    def test_search_compact_counts_follow_navigation(self):
        from kitty.scrollback_search import ScrollbackSearch

        class Window:
            destroyed = False
            os_window_id = id = 1
            def refresh(self):
                pass
            def finish_scroll_animation(self):
                pass
        window = Window()
        window.screen = self.create_screen(cols=20)
        window.screen.draw('cat cat cat')
        with patch('kitty.scrollback_search.set_scrollback_search'), patch('kitty.scrollback_search.wakeup_main_loop'):
            search = window.scrollback_search = ScrollbackSearch(window)
            try:
                self.ae(search.status, '')
                search.query = 'cat'
                search.submitted_revision = search.revision
                search.generation = window.screen.search_generation
                search.matches = ((0, 0, 0, 3), (0, 4, 0, 7), (0, 8, 0, 11))
                search.current = 0
                self.ae(search.status, '1/3')
                search.navigate(-1)
                self.ae(search.status, '3/3')
                search.matches = ()
                self.ae(search.status, '0/0')
            finally:
                search.close()

    def test_search_requires_submission_and_survives_inactive_pane(self):
        from kitty.scrollback_search import ScrollbackSearch

        class Window:
            destroyed = False
            os_window_id = id = 1
            is_visible_in_layout = is_active = False
            def refresh(self):
                pass
            def finish_scroll_animation(self):
                pass

        window = Window()
        window.screen = self.create_screen(cols=20, options={'pixel_scroll': False})
        window.screen.draw('cat cat')
        with patch('kitty.scrollback_search.set_scrollback_search'), patch('kitty.scrollback_search.add_timer', return_value=1) as timer, patch('kitty.scrollback_search.remove_timer'), patch('kitty.scrollback_search.wakeup_main_loop'):
            search = window.scrollback_search = ScrollbackSearch(window)
            try:
                search.show('cat')
                self.ae(search.status, '')
                search.tick(None)
                search.set_option('whole_word', True)
                self.ae(search.status, '')
                search.tick(None)
                self.assertIsNone(search.future)
                timer.assert_not_called()
                search.submit('cat')
                self.ae(search.status, '…')
                search.tick(None)
                search.future.result()
                search.tick(None)
                self.ae(search.matches, ((0, 0, 0, 3), (0, 4, 0, 7)))
                self.ae(search.status, '1/2')
                self.assertFalse(search.closed)
                self.assertIs(window.scrollback_search, search)
                search.submit('cat')
                self.ae(search.current, 1)
                window.screen.draw(' cat')
                search.tick(None)
                self.assertIsNone(search.future)
                self.ae(window.screen.current_selections()[:11], bytes([32] * 3 + [0] + [96] * 3 + [0] * 4))
                self.ae(search.matches, ((0, 0, 0, 3), (0, 4, 0, 7)))
                # Rewritten cells lose their old hits without searching the draft again.
                parse_bytes(window.screen, b'\rno cats here\x1b[K')
                search.tick(None)
                self.assertFalse(any(x & 96 for x in window.screen.current_selections()))
                self.assertFalse(search.closed)
                search.update('new draft')
                self.ae(search.status, '')
                search.tick(None)
                self.assertIsNone(search.future)
                search.submit('new draft')
                search.tick(None)
                search.future.result()
                search.update('missing')
                self.ae(search.status, '')
                search.tick(None)
                self.ae(search.status, '')
                search.submit('missing')
                self.ae(search.status, '…')
                search.tick(None)
                search.future.result()
                search.tick(None)
                self.ae(search.status, '0/0')
                search.set_option('regex', True)
                search.update('[')
                self.assertFalse(search.error)
                search.submit('[')
                self.assertTrue(search.error.startswith('Invalid regex:'))
            finally:
                search.close()

    def test_search_regex_timeout_does_not_block_terminal_process(self):
        import subprocess

        from kitty.scrollback_search import bounded_regex_matches

        text = 'a' * 10000 + '!'
        rows = ((text, tuple(range(len(text) + 1)), False),)
        with self.assertRaises(subprocess.TimeoutExpired):
            bounded_regex_matches(rows, 0, '(a+)+$', False)

    def test_search_discards_stale_worker_and_retains_live_output_matches(self):
        from concurrent.futures import Future

        from kitty.scrollback_search import ScrollbackSearch, find_matches

        class Window:
            destroyed = False
            os_window_id = id = 1
            is_visible_in_layout = is_active = True
            def refresh(self):
                self.screen.mark_as_dirty()
            def finish_scroll_animation(self):
                pass

        window = Window()
        window.screen = screen = self.create_screen(cols=12, lines=2, scrollback=10, options={'pixel_scroll': False})
        parse_bytes(screen, b'needle\r\nlast')
        with patch('kitty.scrollback_search.set_scrollback_search'), patch('kitty.scrollback_search.add_timer', return_value=1), patch('kitty.scrollback_search.remove_timer'), patch('kitty.scrollback_search.wakeup_main_loop'):
            search = window.scrollback_search = ScrollbackSearch(window)
            try:
                search.update('needle')
                search.submit('needle')
                rows = screen.search_rows(0, 2)
                search.future_rows = rows
                search.future_first_row = 0
                search.future_query = search.query
                search.future_revision = search.revision
                search.future_generation = search.generation = screen.search_generation
                search.future_history_added = screen.search_history_added
                future = search.future = Future()
                future.set_result(find_matches(rows, 0, 'needle'))
                parse_bytes(screen, b'\r\nnew output')
                search.tick(None)
                self.ae(search.matches, ((-1, 0, -1, 6),))
                parse_bytes(screen, b'\r\nmore output')
                search.tick(None)
                self.ae(search.matches, ((-2, 0, -2, 6),))
                screen.scroll_to_absolute(2)
                self.ae(screen.current_selections()[:6], bytes([96] * 6))
                search.update('[')
                search.set_option('regex', True)
                self.ae(search.status, '')
                search.submit('[')
                self.assertTrue(search.status.startswith('Invalid regex:'))
                search.set_option('regex', False)
                self.assertFalse(search.error)
                # Same text, new option revision must not publish old results.
                search.update('needle')
                search.submit('needle')
                if search.future is not None:
                    search.future.result()
                future = search.future = Future()
                search.future_query = 'needle'
                search.future_revision = search.revision - 1
                future.set_result(((0, 0, 0, 6),))
                search.tick(None)
                self.ae(search.matches, ())
            finally:
                search.close()

    def test_search_navigation_wraps_preserves_selection_and_cleanup(self):
        from kitty.scrollback_search import ScrollbackSearch

        screen = self.create_screen(lines=3, cols=12, scrollback=30)
        parse_bytes(screen, b'first token\r\nsecond token\r\nthird token\r\nlast')
        screen.start_selection(0, 1)
        screen.update_selection(3, 1)
        before = screen.text_for_selection()
        class Window:
            destroyed = False
            os_window_id = 1
            id = 1
            is_visible_in_layout = True
            is_active = True
            def refresh(self):
                self.screen.mark_as_dirty()
            def finish_scroll_animation(self):
                pass
        window = Window()
        window.screen = screen
        with patch('kitty.scrollback_search.set_scrollback_search'), patch('kitty.scrollback_search.wakeup_main_loop'):
            search = ScrollbackSearch(window)
            window.scrollback_search = search
            search.query = 'token'
            search.generation = screen.search_generation
            search.matches = ((-1, 6, -1, 11), (0, 7, 0, 12), (1, 6, 1, 11))
            search.current = 0
            search.navigate(-1)
            self.ae(search.current, 2)
            search.navigate(1)
            self.ae(search.current, 0)
            self.ae(screen.text_for_selection(), before)
            search.close()
            self.assertIsNone(window.scrollback_search)
            self.ae(screen.text_for_selection(), before)
            self.assertTrue(search.closed)

    def test_search_real_screen_snapshots_match_history_and_wide_cells(self):
        from kitty.scrollback_search import find_matches

        screen = self.create_screen(lines=2, cols=6, scrollback=20)
        parse_bytes(screen, 'ab界cde\r\nend'.encode())
        start = -screen.historybuf.count
        rows = screen.search_rows(start, screen.lines - start)
        matches = find_matches(rows, start, '界cd')
        self.ae(len(matches), 1)
        self.ae(matches[0][1], 2)
        self.ae(matches[0][3], 6)

    def test_search_input_is_bound_to_source_pane(self):
        from kitty.boss import Boss

        calls = []
        first = SimpleNamespace(scrollback_search=SimpleNamespace(update=lambda query: calls.append(('first', query))))
        second = SimpleNamespace(scrollback_search=SimpleNamespace(update=lambda query: calls.append(('second', query))))
        boss = SimpleNamespace(window_id_map={1: first, 2: second}, active_window=second)
        Boss.scrollback_search_event(boss, '[1, "query", "needle"]')
        self.ae(calls, [('first', 'needle')])
        Boss.scrollback_search_event(boss, '[99, "query", "needle"]')
        self.ae(calls, [('first', 'needle')])

    def test_search_navigation_submits_current_draft(self):
        from kitty.boss import Boss
        from kitty.scrollback_search import ScrollbackSearch

        class Window:
            destroyed = False
            os_window_id = id = 1
            def refresh(self):
                pass
            def finish_scroll_animation(self):
                pass

        window = Window()
        boss = SimpleNamespace(window_id_map={1: window})
        with patch('kitty.scrollback_search.set_scrollback_search'), patch('kitty.scrollback_search.add_timer', return_value=1), patch('kitty.scrollback_search.remove_timer'), patch('kitty.scrollback_search.wakeup_main_loop'):
            search = window.scrollback_search = ScrollbackSearch(window)
            try:
                for command in ('next', 'previous'):
                    with self.subTest(command=command):
                        window.screen = self.create_screen(cols=30)
                        window.screen.draw('cat cat dog')
                        search.update('cat')
                        Boss.scrollback_search_event(boss, json.dumps([1, command, 'cat']))
                        search.tick(None)
                        self.assertIsNotNone(search.future)
                        search.future.result()
                        search.tick(None)
                        self.ae(search.matches, ((0, 0, 0, 3), (0, 4, 0, 7)))
                        Boss.scrollback_search_event(boss, json.dumps([1, command, 'cat']))
                        self.ae(search.current, 1)
                        Boss.scrollback_search_event(boss, json.dumps([1, command, 'dog']))
                        search.tick(None)
                        search.future.result()
                        search.tick(None)
                        self.ae(search.matches, ((0, 8, 0, 11),))
                        window.screen.draw(' dog')
                        Boss.scrollback_search_event(boss, json.dumps([1, command, 'dog']))
                        search.tick(None)
                        search.future.result()
                        search.tick(None)
                        self.ae(search.matches, ((0, 8, 0, 11), (0, 12, 0, 15)))
            finally:
                search.close()

    def test_search_opening_and_closing_are_independent_per_pane(self):
        from kitty.boss import Boss
        from kitty.scrollback_search import ScrollbackSearch

        class Window:
            destroyed = False
            os_window_id = 1
            scrollback_search = None
            def refresh(self):
                pass
            def finish_scroll_animation(self):
                pass
            def text_for_selection(self):
                return self.selection

        first, second = Window(), Window()
        first.id, second.id = 1, 2
        first.selection, second.selection = 'cat', 'dog'
        first.screen, second.screen = self.create_screen(cols=30), self.create_screen(cols=30)
        first.screen.draw('cat cat dog')
        second.screen.draw('dog dog cat')
        boss = SimpleNamespace(window_id_map={1: first, 2: second}, active_window=first)
        with patch('kitty.boss.is_macos', True), patch('kitty.scrollback_search.set_scrollback_search') as native, patch('kitty.scrollback_search.add_timer', return_value=1), patch('kitty.scrollback_search.remove_timer'), patch('kitty.scrollback_search.wakeup_main_loop'):
            try:
                Boss.start_direct_scrollback_search(boss, first)
                first_search = first.scrollback_search
                self.assertIsInstance(first_search, ScrollbackSearch)
                Boss.scrollback_search_event(boss, '[1, "case_sensitive", "1"]')
                first_search.tick(None)
                first_search.future.result()
                first_search.tick(None)
                Boss.scrollback_search_event(boss, '[1, "next", "cat"]')
                self.ae(first_search.matches, ((0, 0, 0, 3), (0, 4, 0, 7)))
                self.ae(first_search.current, 1)

                Boss.start_direct_scrollback_search(boss, second)
                second_search = second.scrollback_search
                self.assertIsInstance(second_search, ScrollbackSearch)
                self.assertIsNot(second_search, first_search)
                self.ae(second_search.query, 'dog')
                self.assertFalse(second_search.case_sensitive)
                self.assertIsNone(second_search.future)
                Boss.scrollback_search_event(boss, '[2, "whole_word", "1"]')
                second_search.tick(None)
                second_search.future.result()
                second_search.tick(None)
                self.ae(second_search.matches, ((0, 0, 0, 3), (0, 4, 0, 7)))
                self.ae(second_search.current, 0)

                first.selection = 'replacement'
                Boss.start_direct_scrollback_search(boss, first)
                self.assertIs(first.scrollback_search, first_search)
                self.ae(first_search.query, 'cat')
                self.ae(first_search.current, 1)
                self.ae(first_search.status, '2/2')
                self.assertTrue(first_search.case_sensitive)
                self.assertFalse(first_search.whole_word)
                native.assert_called_with(1, 1, 'cat', '2/2', True, True)
                self.ae(second_search.query, 'dog')
                self.assertTrue(second_search.whole_word)
                self.ae(second_search.current, 0)

                Boss.end_direct_scrollback_search(boss, first)
                self.assertIsNone(first.scrollback_search)
                self.assertTrue(first_search.closed)
                self.assertIs(second.scrollback_search, second_search)
                self.assertFalse(second_search.closed)
                Boss.scrollback_search_event(boss, '[2, "next", "dog"]')
                self.ae(second_search.current, 1)
                self.ae(second_search.status, '2/2')
            finally:
                for window in (first, second):
                    if window.scrollback_search is not None:
                        window.scrollback_search.close()

    def test_search_option_events_submit_current_draft(self):
        from kitty.boss import Boss
        from kitty.scrollback_search import ScrollbackSearch

        class Window:
            destroyed = False
            os_window_id = id = 1
            def refresh(self):
                pass
            def finish_scroll_animation(self):
                pass

        window = Window()
        window.screen = self.create_screen(cols=40)
        window.screen.draw('cat CAT scatter c.t')
        boss = SimpleNamespace(window_id_map={1: window})
        cases = (
            ('case_sensitive', 'cat', ((0, 0, 0, 3), (0, 9, 0, 12)), ((0, 0, 0, 3), (0, 4, 0, 7), (0, 9, 0, 12))),
            ('whole_word', 'cat', ((0, 0, 0, 3), (0, 4, 0, 7)), ((0, 0, 0, 3), (0, 4, 0, 7), (0, 9, 0, 12))),
            ('regex', 'c.t', ((0, 0, 0, 3), (0, 4, 0, 7), (0, 9, 0, 12), (0, 16, 0, 19)), ((0, 16, 0, 19),)),
        )
        with patch('kitty.scrollback_search.set_scrollback_search'), patch('kitty.scrollback_search.add_timer', return_value=1), patch('kitty.scrollback_search.remove_timer'), patch('kitty.scrollback_search.wakeup_main_loop'):
            search = window.scrollback_search = ScrollbackSearch(window)
            try:
                for name, query, enabled_matches, disabled_matches in cases:
                    for enabled, expected in ((True, enabled_matches), (False, disabled_matches)):
                        with self.subTest(option=name, enabled=enabled):
                            search.update(query)
                            self.ae(search.status, '')
                            Boss.scrollback_search_event(boss, json.dumps([1, name, '1' if enabled else '0']))
                            self.ae(search.status, '…')
                            search.tick(None)
                            self.assertIsNotNone(search.future)
                            search.future.result()
                            search.tick(None)
                            self.ae(search.matches, expected)
                            self.ae(search.status, f'1/{len(expected)}')
            finally:
                search.close()

    def test_search_shortcuts_submit_draft_and_changed_screen(self):
        from kitty.boss import Boss
        from kitty.scrollback_search import ScrollbackSearch

        class Window:
            destroyed = False
            os_window_id = id = 1
            def refresh(self):
                pass
            def finish_scroll_animation(self):
                pass

        with patch('kitty.scrollback_search.set_scrollback_search'), patch('kitty.scrollback_search.add_timer', return_value=1), patch('kitty.scrollback_search.remove_timer'), patch('kitty.scrollback_search.wakeup_main_loop'):
            for backwards in (False, True):
                with self.subTest(backwards=backwards):
                    window = Window()
                    window.screen = self.create_screen(cols=30)
                    window.screen.draw('cat cat cat')
                    boss = SimpleNamespace(active_window=window)
                    search = window.scrollback_search = ScrollbackSearch(window)
                    try:
                        search.update('cat')
                        Boss.navigate_direct_scrollback_search(boss, backwards=backwards)
                        search.tick(None)
                        self.assertIsNotNone(search.future)
                        search.future.result()
                        search.tick(None)
                        self.ae(search.matches, ((0, 0, 0, 3), (0, 4, 0, 7), (0, 8, 0, 11)))
                        Boss.navigate_direct_scrollback_search(boss, backwards=backwards)
                        self.ae(search.current, 2 if backwards else 1)
                        self.assertIsNone(search.future)
                        window.screen.draw(' cat')
                        Boss.navigate_direct_scrollback_search(boss, backwards=backwards)
                        search.tick(None)
                        self.assertIsNotNone(search.future)
                        search.future.result()
                        search.tick(None)
                        self.ae(search.matches, ((0, 0, 0, 3), (0, 4, 0, 7), (0, 8, 0, 11), (0, 12, 0, 15)))
                    finally:
                        search.close()
