#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

import unittest

from kitty.constants import is_macos

from .base import BaseTest


@unittest.skipUnless(is_macos, 'Native pane switcher requires macOS')
class TestPaneSwitcher(BaseTest):
    def test_dispatches_configured_shortcut(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        from kitty.boss import Boss

        tab = SimpleNamespace(current_layout=SimpleNamespace(name='stack'))
        tab.next_window = Mock()
        tab.previous_window = Mock()
        mappings = SimpleNamespace(
            keyboard_mode_stack=[],
            keyboard_modes={'': SimpleNamespace(keymap={})},
            global_shortcuts_map={},
        )
        mappings.matching_key_actions = Mock(return_value=[SimpleNamespace(is_sequence=False, definition='next_window')])
        boss = SimpleNamespace(mappings=mappings, active_tab=tab)
        options = SimpleNamespace(
            alias_map=SimpleNamespace(resolve_aliases=Mock(return_value=[SimpleNamespace(func='next_window', args=())])))
        with patch('kitty.keys.get_shortcut', return_value=[object()]), \
                patch('kitty.boss.get_options', return_value=options):
            self.assertTrue(Boss.dispatch_pane_switcher_shortcut(boss, object()))
        tab.next_window.assert_called_once_with()
        tab.previous_window.assert_not_called()

        mappings.matching_key_actions.return_value = [SimpleNamespace(is_sequence=True, definition='next_window')]
        with patch('kitty.keys.get_shortcut', return_value=[object()]), \
                patch('kitty.boss.get_options', return_value=options):
            self.assertFalse(Boss.dispatch_pane_switcher_shortcut(boss, object()))
        tab.next_window.reset_mock()

        tab.current_layout.name = 'splits'
        mappings.matching_key_actions.return_value = [SimpleNamespace(is_sequence=False, definition='next_window')]
        with patch('kitty.keys.get_shortcut', return_value=[object()]), \
                patch('kitty.boss.get_options', return_value=options):
            self.assertFalse(Boss.dispatch_pane_switcher_shortcut(boss, object()))
        tab.next_window.assert_not_called()
