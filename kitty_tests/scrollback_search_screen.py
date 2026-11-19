#!/usr/bin/env python
# License: GPL v3 Copyright: 2026

from .base import BaseTest, draw_multicell, parse_bytes


class TestScrollbackSearchScreen(BaseTest):
    def search_screen(self, cols=12, lines=4, **kw):
        return self.create_screen(cols=cols, lines=lines, options={'pixel_scroll': False}, **kw)

    def test_search_rows_unicode_snapshot(self):
        s = self.search_screen()
        s.draw('a界e\u0301👩\u200d💻')
        snapshot = s.search_rows(0, 1)
        self.ae(snapshot, (('a界e\u0301👩\u200d💻', (0, 1, 3, 3, 4, 4, 4, 6), False),))
        s.carriage_return()
        s.draw('replaced')
        self.ae(snapshot[0][0], 'a界e\u0301👩\u200d💻')
        self.ae(s.search_rows(3, 1), (('', (0,), False),))
        self.ae(s.search_rows(s.lines, 0), ())

    def test_search_rows_tabs_and_overwritten_tab(self):
        s = self.search_screen()
        s.draw('a\tb')
        self.ae(s.search_rows(0, 1), (('a\tb', (0, 1, 8, 9), False),))
        s.cursor_position(1, 5)
        s.draw('X')
        self.ae(s.search_rows(0, 1), (('a\tX   b', (0, 1, 4, 5, 6, 7, 8, 9), False),))

    def test_search_rows_softwrap_and_history(self):
        s = self.search_screen(cols=4, lines=2, scrollback=10)
        s.draw('abcdefghij')
        self.ae(s.historybuf.count, 1)
        self.ae(s.search_rows(-1, 3), (
            ('abcd', (0, 1, 2, 3, 4), False),
            ('efgh', (0, 1, 2, 3, 4), True),
            ('ij', (0, 1, 2), True),
        ))
        s.carriage_return()
        s.linefeed()
        self.assertFalse(s.search_rows(1, 1)[0][2])
        self.ae(len(s.search_rows(-2, 100)), 4)
        s.toggle_alt_screen()
        with self.assertRaises(IndexError):
            s.search_rows(-1, 1)

    def test_search_rows_multicell_continuations(self):
        s = self.search_screen(cols=12)
        draw_multicell(s, 'AB', width=2, scale=2)
        self.ae(s.search_rows(0, 2), (
            ('AB', (0, 0, 4), False),
            ('', (0,), False),
        ))
        s.set_search_matches(((0, 0, 0, 1),), current=0)
        data = s.current_selections()
        self.ae(data[:12], bytes([96] * 4 + [0] * 8))
        self.ae(data[12:24], bytes([96] * 4 + [0] * 8))
        s.cursor_position(s.lines, 1)
        s.linefeed()
        self.ae(s.search_rows(-1, 1)[0][0], 'AB')
        s.set_search_matches(((-1, 0, -1, 1),), current=0)
        self.ae(s.current_selections()[:12], bytes([96] * 4 + [0] * 8))


    def test_search_matches_bits_and_user_selection(self):
        s = self.search_screen(cols=24)
        s.draw('a界bcd http://moo.com')
        s.start_selection(0, 0)
        s.update_selection(4, 0)
        selected_text = s.text_for_selection()
        parse_bytes(s, b'\x1b[>2;2:1:7 q')
        s.detect_url(13, 0)
        before = s.current_selections()
        self.assertTrue(any(x & 2 for x in before))
        self.assertTrue(any(x & 28 for x in before))
        s.set_search_matches(((0, 1, 0, 2), (0, 4, 0, 6)), current=0)
        data = s.current_selections()
        self.ae(bytes(x & 31 for x in data), before)
        self.ae(bytes(x & 96 for x in data[:8]), bytes([0, 96, 96, 0, 32, 32, 0, 0]))
        self.ae(s.text_for_selection(), selected_text)
        s.clear_search()
        self.ae(s.current_selections(), before)
        self.ae(s.text_for_selection(), selected_text)

    def test_search_matches_multiline_and_scrolled_coordinates(self):
        s = self.search_screen(cols=4, lines=2, scrollback=10)
        s.draw('abcdefghijkl')
        s.set_search_matches(((-1, 2, 1, 1),), current=0)
        self.ae(s.current_selections(), bytes([96] * 5 + [0] * 3))
        generation = s.search_generation
        s.scroll(1, True)
        self.ae(s.search_generation, generation)
        self.ae(s.current_selections(), bytes([0, 0] + [96] * 6))
        s.set_search_matches(((-1, 0, -1, 2),))
        self.ae(s.current_selections(), bytes([32, 32] + [0] * 6))

    def test_search_matches_pixel_scroll_leading_row(self):
        s = self.create_screen(cols=4, lines=2, scrollback=10, options={'pixel_scroll': True})
        s.draw('abcdefghij')
        s.set_search_matches(((-1, 1, 0, 2),), current=0)
        self.ae(s.current_selections(), bytes([0, 96, 96, 96, 96, 96, 0, 0, 0, 0, 0, 0]))

    def test_search_reveal_unfreezes_unchanged_scroll_position(self):
        for alternate in (False, True):
            with self.subTest(alternate=alternate):
                s = self.search_screen(cols=4, lines=2, scrollback=10)
                if alternate:
                    s.toggle_alt_screen()
                s.draw('abcdefgh')
                self.assertTrue(s.pause_rendering(True, 5000))
                parse_bytes(s, b'\r\nlive')
                s.set_search_matches(((0, 0, 0, 1),), current=0)
                self.assertFalse(any(x & 96 for x in s.current_selections()))
                # Search navigation explicitly reveals the live frame even
                # when its target already equals the live scroll position.
                s.scroll_to_absolute(s.scrolled_by)
                s.set_search_matches(((0, 0, 0, 1),), current=0)
                self.assertFalse(s.pause_rendering(False))
                self.ae(s.rendered_scroll_state(), (s.scrolled_by, 0, s.historybuf.count))
                self.ae(s.current_selections(), bytes([96] + [0] * 7))

    def test_search_generation_and_invalidation(self):
        s = self.search_screen()
        s.draw('abc')
        generation = s.search_generation
        s.set_search_matches(((0, 0, 0, 2),), current=0)
        s.mark_as_dirty()
        s.start_selection(0, 0)
        s.update_selection(1, 0)
        s.clear_search()
        self.ae(s.search_generation, generation)
        for mutate in (
            lambda: s.draw('x'),
            lambda: s.erase_in_line(2),
            lambda: s.insert_characters(1),
            lambda: s.delete_characters(1),
            lambda: s.draw('\t'),
            lambda: s.resize(5, 10),
            s.clear_scrollback,
            s.toggle_alt_screen,
            s.reset,
        ):
            s.set_search_matches(((0, 0, 0, 1),))
            generation = s.search_generation
            mutate()
            self.assertGreater(s.search_generation, generation)
            self.assertFalse(any(x & 96 for x in s.current_selections()))
        with self.assertRaises(AttributeError):
            s.search_generation = 0

    def test_search_arguments_are_validated_atomically(self):
        s = self.search_screen()
        s.draw('abc')
        s.set_search_matches(((0, 0, 0, 1),), current=0)
        before = s.current_selections()
        for matches, current, error in (
            (((0, 0, 0, 0),), -1, ValueError),
            (((0, -1, 0, 1),), -1, ValueError),
            (((0, 0, 4, 1),), -1, ValueError),
            (((0, 0, 0, 13),), -1, ValueError),
            (((0, 0, 0, 1),), 1, ValueError),
            (((0, 0, 0, 1),), -2, ValueError),
            (((0, 0, 0, 1), (0, 0, 0, 'x')), -1, TypeError),
            (((0, 0, 0, 1 << 80),), -1, OverflowError),
            (((0, 0, 1),), -1, TypeError),
        ):
            with self.subTest(matches=matches, current=current):
                with self.assertRaises(error):
                    s.set_search_matches(matches, current)
                self.ae(s.current_selections(), before)
        for args, error in (((-1, 1), IndexError), ((5, 1), IndexError), ((0, -1), ValueError), ((0, '1'), TypeError)):
            with self.assertRaises(error):
                s.search_rows(*args)
        s.set_search_matches(())
        self.assertFalse(any(x & 96 for x in s.current_selections()))
