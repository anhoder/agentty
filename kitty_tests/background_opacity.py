#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, kitty contributors

from functools import partial
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from kitty.boss import Boss
from kitty.fast_data_types import global_background_opacity
from kitty.options.utils import parse_key_action

from .base import BaseTest


class BackgroundOpacityTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.boss = SimpleNamespace(
            os_window_map={1: None, 2: None, 3: None},
            window_for_dispatch=None,
            active_window=None,
            show_error=Mock(),
        )
        for name in ('_set_background_opacity', '_set_os_window_background_opacity'):
            setattr(self.boss, name, partial(getattr(Boss, name), self.boss))
        self.opts = self.set_options({'dynamic_background_opacity': True, 'background_opacity': 0.7})
        self.enterContext(patch('kitty.boss.get_options', return_value=self.opts))
        self.change = self.enterContext(patch('kitty.boss.change_background_opacity'))
        self.current = self.enterContext(patch('kitty.boss.background_opacity_of', side_effect={1: 0.25, 2: 0.75, 3: 0.95}.get))

    def test_background_opacity_all_parsing(self):
        for name in ('set_background_opacity', 'set_background_opacity_all'):
            for arg in ('+0.1', '-0.1', '0.5', 'default'):
                with self.subTest(name=name, arg=arg):
                    action = parse_key_action(f'{name} {arg}')
                    self.ae(action.func, name)
                    self.ae(action.args, (arg,))

    def test_background_opacity_all_relative(self):
        for arg, expected, default in (
            ('+0.1', (0.8, 0.8, 0.8), 0.8),
            ('-0.3', (0.5, 0.5, 0.5), 0.5),
        ):
            with self.subTest(arg=arg):
                self.change.reset_mock()
                Boss.set_background_opacity_all(self.boss, arg)
                self.ae([c.args[0] for c in self.change.call_args_list], [1, 2, 3])
                for c, opacity in zip(self.change.call_args_list, expected):
                    self.assertAlmostEqual(c.args[1], opacity)
                self.assertAlmostEqual(global_background_opacity(), default)
        self.ae(self.opts.background_opacity, 0.7)
        self.current.assert_not_called()

    def test_background_opacity_all_absolute_and_default(self):
        for arg, expected in (('0.5', 0.5), ('default', 0.7), ('1.5', 1.0)):
            with self.subTest(arg=arg):
                self.change.reset_mock()
                Boss.set_background_opacity_all(self.boss, arg)
                self.ae(self.change.call_args_list, [call(wid, expected) for wid in (1, 2, 3)])
                self.assertAlmostEqual(global_background_opacity(), expected)
        self.current.assert_not_called()
        self.ae(self.opts.background_opacity, 0.7)

    def test_background_opacity_all_missing_window(self):
        self.current.side_effect = {1: 0.25, 3: 0.95}.get
        Boss.set_background_opacity_all(self.boss, '+0.1')
        self.ae([c.args[0] for c in self.change.call_args_list], [1, 2, 3])
        for changed in self.change.call_args_list:
            self.assertAlmostEqual(changed.args[1], 0.8)
        self.assertAlmostEqual(global_background_opacity(), 0.8)
        self.current.assert_not_called()

    def test_background_opacity_requires_dynamic_option(self):
        self.opts.dynamic_background_opacity = False
        self.boss.active_window = SimpleNamespace(os_window_id=1)
        for action in (Boss.set_background_opacity, Boss.set_background_opacity_all):
            with self.subTest(action=action.__name__):
                self.boss.show_error.reset_mock()
                action(self.boss, '+0.1')
                self.boss.show_error.assert_called_once()
        self.change.assert_not_called()
        self.current.assert_not_called()
        self.assertAlmostEqual(global_background_opacity(), 0.7)

    def test_background_opacity_empty_targets_and_argument(self):
        Boss.set_background_opacity(self.boss, '0.5')
        Boss.set_background_opacity_all(self.boss, '')
        self.boss.os_window_map.clear()
        Boss.set_background_opacity_all(self.boss, '0.5')
        self.change.assert_not_called()
        self.boss.show_error.assert_not_called()
        self.assertAlmostEqual(global_background_opacity(), 0.7)

    def test_background_opacity_current_window_unchanged(self):
        self.boss.active_window = SimpleNamespace(os_window_id=1)
        Boss.set_background_opacity(self.boss, '+0.1')
        self.change.assert_called_once_with(1, 0.35)
        self.change.reset_mock()
        self.boss.window_for_dispatch = SimpleNamespace(os_window_id=2)
        Boss.set_background_opacity(self.boss, 'default')
        self.change.assert_called_once_with(2, 0.7)
        self.assertAlmostEqual(global_background_opacity(), 0.7)

    def test_background_opacity_runtime_default_reload(self):
        Boss.set_background_opacity_all(self.boss, '0.4')
        self.assertAlmostEqual(global_background_opacity(), 0.4)
        self.ae(self.opts.background_opacity, 0.7)
        self.set_options({'dynamic_background_opacity': True, 'background_opacity': 0.6})
        self.assertAlmostEqual(global_background_opacity(), 0.6)

    def test_background_opacity_runtime_default_relative_clamping(self):
        for arg, expected in (('-1', 0.0), ('+0.1', 0.1), ('+2', 1.0), ('-0.2', 0.8)):
            Boss.set_background_opacity_all(self.boss, arg)
            self.assertAlmostEqual(global_background_opacity(), expected)
            for changed in self.change.call_args_list[-3:]:
                self.assertAlmostEqual(changed.args[1], expected)
        Boss.set_background_opacity_all(self.boss, 'default')
        self.assertAlmostEqual(global_background_opacity(), 0.7)

    def test_background_opacity_local_change_keeps_runtime_default(self):
        Boss.set_background_opacity_all(self.boss, '0.4')
        self.change.reset_mock()
        self.boss.active_window = SimpleNamespace(os_window_id=1)
        Boss.set_background_opacity(self.boss, 'default')
        self.change.assert_called_once_with(1, 0.7)
        self.assertAlmostEqual(global_background_opacity(), 0.4)
        self.ae(self.opts.background_opacity, 0.7)

    def test_native_tabs_restore_opacity_after_fullscreen(self):
        import os
        import shutil
        import subprocess
        import sys
        import tempfile
        from pathlib import Path

        from kitty.constants import kitty_base_dir

        if not shutil.which('clang'):
            self.skipTest('Native opacity probe requires clang')
        root = Path(os.getcwd() if getattr(sys, 'frozen', False) else kitty_base_dir)
        state = (root / 'kitty/state.h').read_text()
        glfw = (root / 'kitty/glfw.c').read_text()

        def function(source, signature):
            start = source.index(signature)
            return source[start:source.index('\n}', start) + 2]

        prelude = r'''
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
typedef struct {bool fullscreen;} NativeWindow;
typedef struct {
    void *handle;
    bool is_layer_shell;
    struct {float alpha; bool supports_transparency;} background_opacity;
} OSWindow;
static bool glfwIsFullscreen(void *handle, unsigned flags) {
    if (flags != 1) abort();
    return ((NativeWindow *)handle)->fullscreen;
}
'''
        scenario = r'''
static void require(bool ok, const char *message) {
    if (!ok) {fprintf(stderr, "%s\n", message); exit(1);}
}
int main(void) {
    NativeWindow native[2] = {{false}, {false}};
    OSWindow tabs[2] = {
        {.handle=native, .background_opacity={.alpha=0.7f, .supports_transparency=true}},
        {.handle=native+1, .background_opacity={.alpha=0.8f, .supports_transparency=true}},
    };
    for (unsigned i=0; i<2; i++) {
        require(effective_os_window_alpha(tabs+i)==tabs[i].background_opacity.alpha, "windowed tab lost opacity");
        native[i].fullscreen=true;
#ifdef __APPLE__
        require(effective_os_window_alpha(tabs+i)==1.f, "native fullscreen must remain opaque");
#else
        require(effective_os_window_alpha(tabs+i)==tabs[i].background_opacity.alpha, "non-Cocoa fullscreen changed opacity");
#endif
        // AppKit exits the whole group without invoking each tab's toggle intercept.
        native[i].fullscreen=false;
        tabs[i].background_opacity.alpha=0.6f;
        require(effective_os_window_alpha(tabs+i)==0.6f, "tab retained opaque fullscreen state after group exit");
    }
    tabs[0].background_opacity.supports_transparency=false;
    require(effective_os_window_alpha(tabs)==1.f, "unsupported framebuffer became transparent");
    tabs[1].is_layer_shell=true;
    native[1].fullscreen=true;
    require(effective_os_window_alpha(tabs+1)==0.6f, "layer shell inherited native fullscreen opacity");
    tabs[1].handle=NULL;
    require(effective_os_window_alpha(tabs+1)==0.6f, "missing native handle changed opacity");
    return 0;
}
'''
        code = prelude + function(glfw, 'bool\nos_window_has_opaque_fullscreen(')
        code += '\n' + function(state, 'static inline float\neffective_os_window_alpha(') + scenario
        with tempfile.TemporaryDirectory(prefix='opacity-') as directory:
            source, executable = Path(directory) / 'probe.c', Path(directory) / 'probe'
            source.write_text(code)
            for platform in ('-D__APPLE__', '-U__APPLE__'):
                with self.subTest(platform=platform):
                    compiled = subprocess.run(['clang', platform, str(source), '-o', str(executable)], capture_output=True, text=True)
                    self.ae(compiled.returncode, 0, compiled.stdout + compiled.stderr)
                    result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=5)
                    self.ae(result.returncode, 0, result.stdout + result.stderr)
