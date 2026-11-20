#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

import ctypes as C
import platform
import unittest

from kitty.constants import is_macos

from .base import BaseTest


@unittest.skipUnless(is_macos, 'Native sidebar sections require macOS')
class TestSidebarSections(BaseTest):
    def test_native_sections(self):
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
            symbol = 'objc_msgSend_stret' if result is Rect and platform.machine() == 'x86_64' else 'objc_msgSend'
            if symbol.endswith('_stret'):
                value = Rect()
                C.CFUNCTYPE(None, C.POINTER(Rect), ptr, ptr, *types)((symbol, objc))(
                    C.byref(value), obj, objc.sel_registerName(selector.encode()), *args)
                return value
            return C.CFUNCTYPE(result, ptr, ptr, *types)((symbol, objc))(
                obj, objc.sel_registerName(selector.encode()), *args)

        def string(text):
            return send(cls('NSString'), 'stringWithUTF8String:', types=(C.c_char_p,), args=(text.encode(),))

        def text(obj):
            return send(obj, 'UTF8String', C.c_char_p).decode()

        def get(obj, key):
            return send(obj, 'valueForKey:', types=(ptr,), args=(string(key),))

        def number(value):
            return send(cls('NSNumber'), 'numberWithDouble:', types=(C.c_double,), args=(value,))

        def set_value(obj, key, value):
            send(obj, 'setValue:forKey:', None, (ptr, ptr), (number(value), string(key)))

        send(cls('NSApplication'), 'sharedApplication')
        pool = send(send(cls('NSAutoreleasePool'), 'alloc'), 'init')
        self.addCleanup(lambda: send(pool, 'drain', None))
        prefs = send(cls('NSUserDefaults'), 'standardUserDefaults')
        keys = [f'kitty.agents-sidebar.sections.v1.{suffix}' for suffix in (
            'agents-collapsed', 'editors-collapsed', 'agents-fraction')]
        saved = [send(prefs, 'objectForKey:', types=(ptr,), args=(string(key),)) for key in keys]
        for value in saved:
            if value:
                send(value, 'retain')

        def restore():
            for key, value in zip(keys, saved):
                if value:
                    send(prefs, 'setObject:forKey:', None, (ptr, ptr), (value, string(key)))
                    send(value, 'release', None)
                else:
                    send(prefs, 'removeObjectForKey:', None, (ptr,), (string(key),))

        self.addCleanup(restore)
        view = send(send(cls('KittyAgentsSidebarView'), 'alloc'), 'initWithOSWindowID:width:cellWidth:',
                    types=(C.c_uint, C.c_double, C.c_uint), args=(0, 260, 8))
        self.assertTrue(view)
        self.addCleanup(lambda: send(view, 'release', None))

        def rows(agent_id=7, editor_id=7, label=b'nvim'):
            ids = (C.c_ulonglong * 2)(agent_id, editor_id)
            strings = [(C.c_char_p * 2)(*values) for values in (
                (b'', b''), (b'', b''), (b'claude', b''), (b'Agent', label), (b'', b''))]
            states = (C.c_ubyte * 2)(2, 0)
            active = (C.c_bool * 2)(False, False)
            kinds = (C.c_char_p * 2)(b'agent', b'editor')
            send(view, 'setRows:groups:groupKeys:agents:labels:details:states:active:count:kinds:', None,
                 (C.POINTER(C.c_ulonglong),) + (C.POINTER(C.c_char_p),) * 5 +
                 (C.POINTER(C.c_ubyte), C.POINTER(C.c_bool), C.c_size_t, C.POINTER(C.c_char_p)),
                 (ids, *strings, states, active, 2, kinds))

        def entry(key, identity):
            return send(get(view, key), 'objectForKey:', types=(ptr,), args=(
                send(cls('NSNumber'), 'numberWithUnsignedLongLong:', types=(C.c_ulonglong,), args=(identity,)),))

        rows()
        editor_list = get(view, 'editor_outline')
        first_editor_rect = send(editor_list, 'rectOfRow:', Rect, (C.c_long,), (0,))
        self.assertAlmostEqual(first_editor_rect.origin.y, 0)
        editor_row_view = send(editor_list, 'rowViewAtRow:makeIfNecessary:',
                               types=(C.c_long, C.c_bool), args=(0, True))
        if editor_row_view:
            decoration = send(editor_row_view, 'decorationRect', Rect)
            self.assertAlmostEqual(decoration.origin.x, 0)
            self.assertAlmostEqual(decoration.origin.y, 0)
            self.assertAlmostEqual(decoration.size.height, 48)
        send(view, 'setGrouping:', None, (ptr,), (string('tab'),))
        rows()
        self.ae(text(send(get(view, 'agents_header'), 'title')), 'Grouping: tab · 1 agents · 0 groups')
        agent, editor = entry('entries_by_id', 7), entry('editors_by_id', 7)
        self.assertTrue(agent)
        self.assertTrue(editor)
        self.assertNotEqual(agent, editor)
        outline = get(view, 'outline')
        cell = send(outline, 'viewAtColumn:row:makeIfNecessary:', types=(C.c_long, C.c_long, C.c_bool), args=(0, 0, True))
        rows(label=b'Updated editor')
        self.ae(entry('entries_by_id', 7), agent)
        self.ae(entry('editors_by_id', 7), editor)
        if cell:
            self.ae(send(outline, 'viewAtColumn:row:makeIfNecessary:', types=(C.c_long, C.c_long, C.c_bool), args=(0, 0, True)), cell)
        self.assertFalse(send(view, 'workingSpinnerColorForNode:', types=(ptr,), args=(editor,)))
        rows(0, 0)
        self.ae(text(send(get(view, 'agents_header'), 'title')), 'Grouping: tab · 0 agents · 0 groups')
        self.ae(send(get(view, 'editor_outline'), 'numberOfRows', C.c_long), 0)
        self.ae(send(get(view, 'outline'), 'numberOfRows', C.c_long), 0)
        self.ae(text(send(get(view, 'outline'), 'emptyStateText')), 'No Agents')
        self.ae(text(send(get(view, 'editor_outline'), 'emptyStateText')), 'No Editors')
        # Header tracking must set the native hand cursor, not merely register
        # a cursor rect that another AppKit tracking region can override.
        for key in ('agents_header', 'editors_header'):
            header = get(view, key)
            send(header, 'updateTrackingAreas', None)
            self.assertTrue(send(header, 'headerTrackingArea'))
            for event in ('mouseEntered:', 'mouseMoved:', 'cursorUpdate:'):
                send(header, event, None, (ptr,), (None,))
                self.ae(send(cls('NSCursor'), 'currentCursor'), send(cls('NSCursor'), 'pointingHandCursor'))
            send(header, 'mouseExited:', None, (ptr,), (None,))
        empty_agent, empty_editor = entry('entries_by_id', 0), entry('editors_by_id', 0)
        self.assertTrue(empty_agent)
        self.assertTrue(empty_editor)
        self.assertNotEqual(empty_agent, empty_editor)
        rows(0, 0, b'Empty editor')
        self.ae(entry('entries_by_id', 0), empty_agent)
        self.ae(entry('editors_by_id', 0), empty_editor)

        set_value(view, 'agents_collapsed', 0)
        set_value(view, 'editors_collapsed', 0)
        set_value(view, 'agents_fraction', 0.62)

        def layout(height):
            send(view, 'setFrameSize:', None, (Size,), (Size(260, height),))

        def frame(key):
            return send(get(view, key), 'frame', Rect)

        layout(500)
        for key in ('scroll_view', 'editor_scroll_view'):
            self.ae(send(get(view, key), 'borderType', C.c_ulong), 0)
        for key in ('agents_header', 'editors_header'):
            self.assertAlmostEqual(frame(key).origin.x, 0)
            # Only the existing 2pt right-edge resize/focus rail is reserved.
            self.assertAlmostEqual(frame(key).size.width, 258)
            self.assertTrue(send(get(view, key), 'expanded', C.c_bool))
        self.assertTrue(send(get(view, 'agents_header'), 'agentsSection', C.c_bool))
        self.assertFalse(send(get(view, 'editors_header'), 'agentsSection', C.c_bool))
        before = frame('editor_scroll_view').size.height
        send(view, 'toggleAgentsSection:', None, (ptr,), (None,))
        self.assertFalse(send(get(view, 'agents_header'), 'expanded', C.c_bool))
        self.assertTrue(send(get(view, 'scroll_view'), 'isHidden', C.c_bool))
        self.assertAlmostEqual(frame('agents_header').size.height, 30)
        self.assertAlmostEqual(frame('editors_header').origin.y,
                               frame('agents_header').origin.y + frame('agents_header').size.height)
        self.assertGreater(frame('editor_scroll_view').size.height, before)
        self.assertLessEqual(frame('agents_header').origin.y + frame('agents_header').size.height, frame('editors_header').origin.y)
        send(view, 'toggleAgentsSection:', None, (ptr,), (None,))
        self.assertTrue(send(get(view, 'agents_header'), 'expanded', C.c_bool))
        send(view, 'toggleEditorsSection:', None, (ptr,), (None,))
        self.assertTrue(send(get(view, 'editor_scroll_view'), 'isHidden', C.c_bool))
        self.assertGreater(frame('scroll_view').size.height, 300)
        send(view, 'toggleEditorsSection:', None, (ptr,), (None,))
        for height in (0, 1, 20, 52, 500):
            layout(height)
            previous_bottom = 0
            for key in ('agents_header', 'scroll_view', 'section_divider', 'editors_header', 'editor_scroll_view'):
                rect = frame(key)
                self.assertGreaterEqual(rect.size.height, 0)
                self.assertGreaterEqual(rect.origin.y, previous_bottom)
                previous_bottom = rect.origin.y + rect.size.height
                self.assertLessEqual(previous_bottom, height + 0.001)

        layout(500)
        set_value(view, 'dragging_divider', 1)
        set_value(view, 'divider_drag_origin', 0)
        set_value(view, 'divider_start_fraction', 0.62)
        # Window coordinates run opposite to this flipped sidebar's local y.
        for y, expected in ((10000, 0.12), (-10000, 0.88)):
            event = send(cls('NSEvent'), 'mouseEventWithType:location:modifierFlags:timestamp:windowNumber:context:eventNumber:clickCount:pressure:',
                         types=(C.c_ulong, Point, C.c_ulong, C.c_double, C.c_long, ptr, C.c_long, C.c_long, C.c_float),
                         args=(6, Point(20, y), 0, 0, 0, None, 0, 1, 0))
            send(view, 'mouseDragged:', None, (ptr,), (event,))
            self.assertAlmostEqual(send(get(view, 'agents_fraction'), 'doubleValue', C.c_double), expected)

        # An ID-zero agent placeholder must not steal initial keyboard focus
        # from a real, active editor in the expanded lower section.
        rows(0, 7)
        active_editor = entry('editors_by_id', 7)
        set_value(active_editor, 'active', 1)
        window = send(send(cls('NSWindow'), 'alloc'), 'initWithContentRect:styleMask:backing:defer:',
                      types=(Rect, C.c_ulong, C.c_ulong, C.c_bool), args=(Rect(Point(0, 0), Size(260, 500)), 0, 2, False))

        def release_window():
            send(window, 'setContentView:', None, (ptr,), (None,))
            send(window, 'release', None)

        self.addCleanup(release_window)
        send(window, 'setContentView:', None, (ptr,), (view,))
        send(view, 'focusSidebar', None)
        editor_outline = get(view, 'editor_outline')
        self.ae(send(window, 'firstResponder'), editor_outline)
        self.ae(send(view, 'navigationOutline'), editor_outline)
        selected_row = send(editor_outline, 'selectedRow', C.c_long)
        self.assertGreaterEqual(selected_row, 0)
        self.ae(send(editor_outline, 'itemAtRow:', types=(C.c_long,), args=(selected_row,)), active_editor)
