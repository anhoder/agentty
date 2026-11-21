#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

import ctypes as C
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

    def test_modifier_release_and_preview_fill(self):
        # Exercise the loaded production AppKit classes, not a copy of their code.
        from kitty import fast_data_types

        del fast_data_types
        C.CDLL('/System/Library/Frameworks/Cocoa.framework/Cocoa')
        objc = C.CDLL('/usr/lib/libobjc.A.dylib')
        ptr = C.c_void_p
        objc.objc_getClass.argtypes = [C.c_char_p]
        objc.objc_getClass.restype = ptr
        objc.sel_registerName.argtypes = [C.c_char_p]
        objc.sel_registerName.restype = ptr

        class Point(C.Structure):
            _fields_ = [('x', C.c_double), ('y', C.c_double)]

        class Size(C.Structure):
            _fields_ = [('width', C.c_double), ('height', C.c_double)]

        class Rect(C.Structure):
            _fields_ = [('origin', Point), ('size', Size)]

        def cls(name):
            return objc.objc_getClass(name.encode())

        def send(obj, selector, result=ptr, types=(), args=()):
            return C.CFUNCTYPE(result, ptr, ptr, *types)(('objc_msgSend', objc))(
                obj, objc.sel_registerName(selector.encode()), *args)

        def string(text):
            return send(cls('NSString'), 'stringWithUTF8String:', types=(C.c_char_p,), args=(text.encode(),))

        def set_value(obj, key, value):
            send(obj, 'setValue:forKey:', None, (ptr, ptr), (value, string(key)))

        def integer(n):
            return send(cls('NSNumber'), 'numberWithUnsignedLong:', types=(C.c_ulong,), args=(n,))

        def rect(x, y, w, h):
            return Rect(Point(x, y), Size(w, h))

        send(cls('NSApplication'), 'sharedApplication')
        # Isolate only native window validity and the final Python callback.
        # Event handling and production image drawing remain unchanged.
        objc.objc_allocateClassPair.argtypes = [ptr, C.c_char_p, C.c_size_t]
        objc.objc_allocateClassPair.restype = ptr
        objc.class_addMethod.argtypes = [ptr, ptr, ptr, C.c_char_p]
        objc.objc_registerClassPair.argtypes = [ptr]
        subclass = objc.objc_allocateClassPair(cls('KittyPaneSwitcher'), b'KittyPaneSwitcherRegression', 0)
        self.assertTrue(subclass)
        finished = []
        valid = C.CFUNCTYPE(C.c_bool, ptr, ptr)(lambda obj, selector: True)
        finish = C.CFUNCTYPE(None, ptr, ptr, C.c_bool)(lambda obj, selector, confirmed: finished.append(confirmed))
        objc.class_addMethod(subclass, objc.sel_registerName(b'isValid'), valid, b'B@:')
        objc.class_addMethod(subclass, objc.sel_registerName(b'finish:'), finish, b'v@:B')
        objc.objc_registerClassPair(subclass)
        view = send(send(subclass, 'alloc'), 'initWithFrame:', types=(Rect,), args=(rect(0, 0, 600, 400),))
        self.addCleanup(lambda: send(view, 'release', None))
        window = send(send(cls('NSWindow'), 'alloc'), 'initWithContentRect:styleMask:backing:defer:',
                      types=(Rect, C.c_ulong, C.c_ulong, C.c_bool), args=(rect(0, 0, 20, 20), 0, 2, False))
        self.addCleanup(lambda: send(window, 'release', None))

        def event(kind, flags=0, code=55):
            return send(cls('NSEvent'),
                        'keyEventWithType:location:modifierFlags:timestamp:windowNumber:context:characters:charactersIgnoringModifiers:isARepeat:keyCode:',
                        types=(C.c_ulong, Point, C.c_ulong, C.c_double, C.c_long, ptr, ptr, ptr, C.c_bool, C.c_ushort),
                        args=(kind, Point(0, 0), flags, 0, send(window, 'windowNumber', C.c_long), None, string(''), string(''), False, code))

        for modifier in (1 << 17, 1 << 18, 1 << 19, 1 << 20, (1 << 18) | (1 << 20)):
            set_value(view, 'release_modifiers', integer(modifier))
            finished.clear()
            send(view, 'handleEvent:', types=(ptr,), args=(event(12, modifier),))
            self.ae(finished, [])
            send(view, 'handleEvent:', types=(ptr,), args=(event(12),))
            self.ae(finished, [True])
        set_value(view, 'release_modifiers', integer(1 << 20))
        for key in (36, 76, 53):
            finished.clear()
            send(view, 'handleEvent:', types=(ptr,), args=(event(10, 1 << 20, key),))
            self.ae(finished, [])
        set_value(view, 'release_modifiers', integer(0))
        set_value(view, 'initiating_key', integer(48))
        finished.clear()
        # Key-up belongs to this switcher only when it has no foreign window.
        release = event(11, 0, 48)
        set_value(view, 'hosting_window', window)
        send(view, 'handleEvent:', types=(ptr,), args=(release,))
        self.ae(finished, [True])

        entry = send(send(cls('KittyPaneSwitcherEntry'), 'alloc'), 'init')
        entries = send(cls('NSMutableArray'), 'arrayWithObject:', types=(ptr,), args=(entry,))
        set_value(view, 'entries', entries)
        set_value(view, 'columns', integer(1))
        set_value(view, 'tile_width', integer(220))
        set_value(view, 'tile_height', integer(144))
        grid = send(send(cls('KittyPaneSwitcherGrid'), 'alloc'), 'initWithFrame:', types=(Rect,), args=(rect(0, 0, 220, 144),))
        set_value(grid, 'owner', view)
        self.addCleanup(lambda: send(grid, 'release', None))
        send(entry, 'release', None)
        set_value(entry, 'title', string('Preview'))
        for width, height in ((40, 200), (200, 40), (100, 100)):
            image = send(send(cls('NSImage'), 'alloc'), 'initWithSize:', types=(Size,), args=(Size(width, height),))
            send(image, 'lockFocus', None)
            red = send(cls('NSColor'), 'colorWithSRGBRed:green:blue:alpha:', types=(C.c_double,) * 4, args=(1, 0, 0, 1))
            send(red, 'setFill', None)
            appkit = C.CDLL('/System/Library/Frameworks/AppKit.framework/AppKit')
            fill = C.CFUNCTYPE(None, Rect)(('NSRectFill', appkit))
            fill(rect(0, 0, width, height))
            send(image, 'unlockFocus', None)
            set_value(entry, 'image', image)
            send(image, 'release', None)
            bitmap = send(send(cls('NSBitmapImageRep'), 'alloc'),
                          'initWithBitmapDataPlanes:pixelsWide:pixelsHigh:bitsPerSample:samplesPerPixel:hasAlpha:isPlanar:colorSpaceName:bytesPerRow:bitsPerPixel:',
                          types=(ptr, C.c_long, C.c_long, C.c_long, C.c_long, C.c_bool, C.c_bool, ptr, C.c_long, C.c_long),
                          args=(None, 220, 144, 8, 4, True, False, string('NSCalibratedRGBColorSpace'), 0, 0))
            context = send(cls('NSGraphicsContext'), 'graphicsContextWithBitmapImageRep:', types=(ptr,), args=(bitmap,))
            send(cls('NSGraphicsContext'), 'saveGraphicsState', None)
            send(cls('NSGraphicsContext'), 'setCurrentContext:', None, (ptr,), (context,))
            transform = send(cls('NSAffineTransform'), 'transform')
            send(transform, 'translateXBy:yBy:', None, (C.c_double, C.c_double), (0, 144))
            send(transform, 'scaleXBy:yBy:', None, (C.c_double, C.c_double), (1, -1))
            send(transform, 'concat', None)
            send(grid, 'drawRect:', None, (Rect,), (rect(0, 0, 220, 144),))
            send(cls('NSGraphicsContext'), 'restoreGraphicsState', None)
            for x, y in ((15, 15), (204, 15), (15, 94), (204, 94)):
                color = send(bitmap, 'colorAtX:y:', types=(C.c_long, C.c_long), args=(x, y))
                self.assertGreater(send(color, 'redComponent', C.c_double), 0.95)
                self.assertLess(send(color, 'greenComponent', C.c_double), 0.05)
            send(bitmap, 'release', None)
