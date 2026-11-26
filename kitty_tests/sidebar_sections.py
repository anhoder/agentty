#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

import os
import sys

from kitty.constants import kitty_base_dir

from .base import BaseTest


class TestSidebarOpacitySourceContract(BaseTest):
    """Source-level guardrails only; this does not exercise AppKit compositing."""

    def test_sidebar_opacity_uses_window_chrome_alpha(self):
        root = os.getcwd() if getattr(sys, 'frozen', False) else kitty_base_dir
        with open(os.path.join(root, 'kitty', 'cocoa_window.m'), encoding='utf-8') as f:
            cocoa = f.read()
        with open(os.path.join(root, 'kitty', 'glfw.c'), encoding='utf-8') as f:
            glfw = f.read()
        with open(os.path.join(root, 'kitty', 'state.h'), encoding='utf-8') as f:
            state = f.read()

        self.assertIn('effective_os_window_alpha(osw)', cocoa)
        self.assertIn('effective_os_window_alpha(w)', glfw)
        self.assertIn('if (!w->handle || w->is_layer_shell) return;', glfw)
        self.assertLess(glfw.index('cocoa_update_agents_sidebar_opacity'), glfw.index('memcmp(&new_state'))

        setter_start = cocoa.index('- (void)setBackgroundOpacity:(CGFloat)opacity {')
        setter = cocoa[setter_start:cocoa.index('\n}', setter_start) + 2]
        self.assertIn('MAX(0.0, MIN(1.0, opacity))', setter)
        self.assertIn('background_opacity = NAN;', cocoa)
        self.assertIn('background_view.alphaValue = background_opacity >= 1.0 ? 1.0 : 0.0', setter)

        # Opacity multiplies only the section/header decoration fills; text and
        # icons retain their own rendering and the view itself is never faded.
        self.assertIn('band.alphaComponent * self.backgroundOpacity', cocoa)
        self.assertIn('tint.alphaComponent * self.backgroundOpacity', cocoa)
        self.assertNotIn('self.alphaValue = background_opacity', cocoa)
        self.assertIn('colorWithAlphaComponent:separator.alphaComponent * self.backgroundOpacity', cocoa)
        self.assertIn('[sidebar setBackgroundOpacity:osw ? effective_os_window_alpha(osw) : 1.0]', cocoa)
        self.assertIn('if (!enabled)', cocoa)


class TestSidebarSourcePaths(BaseTest):
    def test_frozen_source_paths_without_file(self):
        from unittest.mock import patch

        from . import sidebar_resize

        # bypy runs frozen tests with cwd set to its writable src copy.
        root = os.getcwd() if getattr(sys, 'frozen', False) else kitty_base_dir
        with patch.object(sys, 'frozen', True, create=True), patch('os.getcwd', return_value=root):
            for test in (
                TestSidebarOpacitySourceContract('test_sidebar_opacity_uses_window_chrome_alpha'),
                sidebar_resize.TestSidebarResizeInteraction('test_native_drag_previews_without_committing_until_release'),
            ):
                method = getattr(test, test._testMethodName)
                with self.subTest(test=test.id()), patch.dict(
                    method.__globals__, {'__file__': None, 'kitty_base_dir': '/unavailable/frozen'}
                ):
                    method()
