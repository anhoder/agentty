#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

import ctypes as C
import os
import platform
import sys
import unittest

from kitty.constants import is_macos, kitty_base_dir

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
        self.assertIn('!w->background_opacity.supports_transparency || w->background_opacity.os_forces_opaque', state)

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

        application = send(cls('NSApplication'), 'sharedApplication')
        activation_policy = send(application, 'activationPolicy', C.c_long)
        send(application, 'setActivationPolicy:', None, (C.c_long,), (2,))
        self.addCleanup(lambda: send(application, 'setActivationPolicy:', None, (C.c_long,), (activation_policy,)))
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
        icons_dir = os.path.join(kitty_base_dir, 'kitty', 'agent_icons')
        send(view, 'setValue:forKey:', None, (ptr, ptr), (string(icons_dir), string('iconsDir')))
        for editor in ('nvim', 'vim', 'vi', 'view', 'nano', 'micro', 'helix', 'kakoune', 'emacs'):
            icon = send(view, 'iconForEditor:textColor:', types=(ptr, ptr),
                        args=(string(editor), send(cls('NSColor'), 'labelColor')))
            self.assertTrue(icon, editor)

        def rows(agent_id=7, editor_id=7, label=b'nvim', agent_project_key=b'/tmp/project',
                 editor_project_key=b'/tmp/project'):
            ids = (C.c_ulonglong * 2)(agent_id, editor_id)
            strings = [(C.c_char_p * 2)(*values) for values in (
                (b'', b''), (b'', b''), (b'claude', b'vim'), (b'Agent', label), (b'', b''),
                (agent_project_key, editor_project_key))]
            states = (C.c_ubyte * 2)(2, 0)
            active = (C.c_bool * 2)(False, False)
            kinds = (C.c_char_p * 2)(b'agent', b'editor')
            send(view, 'setRows:groups:groupKeys:agents:labels:details:projectKeys:states:active:count:kinds:', None,
                 (C.POINTER(C.c_ulonglong),) + (C.POINTER(C.c_char_p),) * 6 +
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
        agent, editor = entry('entries_by_id', 7), entry('editors_by_id', 7)
        self.assertTrue(agent)
        self.assertTrue(editor)
        self.assertNotEqual(agent, editor)
        outline = get(view, 'outline')
        editor_outline = get(view, 'editor_outline')

        def link_state(entry, key):
            return send(get(entry, key), 'boolValue', C.c_bool)

        def selection_note(outline_view):
            return send(cls('NSNotification'), 'notificationWithName:object:', types=(ptr, ptr),
                        args=(string('NSOutlineViewSelectionDidChangeNotification'), outline_view))

        def select_entry(outline_view, item):
            row = send(outline_view, 'rowForItem:', C.c_long, (ptr,), (item,))
            indexes = send(cls('NSIndexSet'), 'indexSetWithIndex:', types=(C.c_ulong,), args=(C.c_ulong(row),))
            send(outline_view, 'selectRowIndexes:byExtendingSelection:', None, (ptr, C.c_bool), (indexes, False))
            send(view, 'outlineViewSelectionDidChange:', None, (ptr,), (selection_note(outline_view),))

        # Selecting a row links the same-project row in the other section.
        agent_identity = get(agent, 'identity')
        select_entry(outline, agent_identity)
        self.assertTrue(link_state(agent, 'linked'))
        self.assertTrue(link_state(editor, 'related'))
        self.assertFalse(link_state(editor, 'linked'))
        self.assertGreaterEqual(send(outline, 'selectedRow', C.c_long), 0)
        self.ae(send(editor_outline, 'selectedRow', C.c_long), -1)

        select_entry(editor_outline, editor)
        self.assertTrue(link_state(editor, 'linked'))
        self.assertTrue(link_state(agent, 'related'))
        self.assertFalse(link_state(agent, 'linked'))
        self.assertGreaterEqual(send(editor_outline, 'selectedRow', C.c_long), 0)
        self.ae(send(outline, 'selectedRow', C.c_long), -1)
        # Rebuilding the agent outline must not restore a stale agent selection.
        send(view, 'rebuildOutline', None, ())
        self.ae(send(outline, 'selectedRow', C.c_long), -1)
        self.assertGreaterEqual(send(editor_outline, 'selectedRow', C.c_long), 0)
        self.assertTrue(link_state(editor, 'linked'))
        self.assertTrue(link_state(agent, 'related'))

        # Different project keys must not link.
        rows(agent_project_key=b'/tmp/a', editor_project_key=b'/tmp/b')
        select_entry(outline, agent_identity)
        self.assertFalse(link_state(agent, 'linked'))
        self.assertFalse(link_state(editor, 'related'))
        self.assertFalse(link_state(editor, 'linked'))
        rows()
        select_entry(editor_outline, editor)
        rows(agent_id=8, editor_id=7)
        self.assertGreaterEqual(send(editor_outline, 'selectedRow', C.c_long), 0)
        self.assertTrue(link_state(entry('editors_by_id', 7), 'linked'))
        self.assertTrue(link_state(entry('entries_by_id', 8), 'related'))

        def tab_rows(active_editor_id, label=b'Editor B', state=2):
            ids = (C.c_ulonglong * 5)(8, 18, 28, 7, 17)
            strings = [(C.c_char_p * 5)(*values) for values in (
                (b'Tab A', b'Tab B', b'Tab B', b'Tab A', b'Tab B'),
                (b'tab-a', b'tab-b', b'tab-b', b'tab-a', b'tab-b'),
                (b'claude', b'claude', b'opencode', b'vim', b'vim'),
                (b'Agent A', b'Agent B', b'Agent B2', b'Editor A', label), (b'', b'', b'', b'', b''),
                (b'/tmp/a', b'/tmp/b', b'/tmp/b', b'/tmp/a', b'/tmp/b'))]
            states = (C.c_ubyte * 5)(2, state, 1, 0, 0)
            active = (C.c_bool * 5)(False, False, False, active_editor_id == 7, active_editor_id == 17)
            kinds = (C.c_char_p * 5)(b'agent', b'agent', b'agent', b'editor', b'editor')
            send(view, 'setRows:groups:groupKeys:agents:labels:details:projectKeys:states:active:count:kinds:', None,
                 (C.POINTER(C.c_ulonglong),) + (C.POINTER(C.c_char_p),) * 6 +
                 (C.POINTER(C.c_ubyte), C.POINTER(C.c_bool), C.c_size_t, C.POINTER(C.c_char_p)),
                 (ids, *strings, states, active, 5, kinds))

        def assert_editor_source(identity):
            selected_row = send(editor_outline, 'selectedRow', C.c_long)
            if identity:
                self.assertGreaterEqual(selected_row, 0)
                self.ae(send(editor_outline, 'itemAtRow:', types=(C.c_long,), args=(selected_row,)),
                        entry('editors_by_id', identity))
            else:
                self.ae(selected_row, -1)
            self.ae(send(outline, 'selectedRow', C.c_long), -1)
            self.ae(send(get(view, 'link_source_window_id'), 'unsignedLongLongValue', C.c_ulonglong), identity)
            self.ae(link_state(view, 'link_source_is_editor'), bool(identity))
            for agent_id, editor_id in ((8, 7), (18, 17)):
                agent_entry = entry('entries_by_id', agent_id)
                editor_entry = entry('editors_by_id', editor_id)
                self.ae(link_state(agent_entry, 'related'), editor_id == identity)
                self.assertFalse(link_state(agent_entry, 'linked'))
                self.ae(link_state(editor_entry, 'linked'), editor_id == identity)
                self.assertFalse(link_state(editor_entry, 'related'))

        # Rows from both tabs remain visible. An activation refresh must replace
        # the previous tab's selected editor and same-project relation source.
        tab_rows(7)
        select_entry(editor_outline, entry('editors_by_id', 7))
        assert_editor_source(7)
        tab_rows(17)
        send(view, 'syncEditorSelectionToWindowID:', None, (C.c_ulonglong,), (17,))
        assert_editor_source(17)

        # Ordinary title/status refreshes must not overwrite a manual selection,
        # even when a different editor carries the active-window flag.
        select_entry(editor_outline, entry('editors_by_id', 7))
        tab_rows(17, label=b'Updated editor B', state=3)
        assert_editor_source(7)

        # Explicit activation also replaces an Agents-section source and clears
        # its remembered selection, so rebuilding cannot resurrect that source.
        select_entry(outline, get(entry('entries_by_id', 8), 'identity'))
        self.assertTrue(link_state(entry('entries_by_id', 8), 'linked'))
        self.assertTrue(link_state(entry('editors_by_id', 7), 'related'))
        send(view, 'syncEditorSelectionToWindowID:', None, (C.c_ulonglong,), (17,))
        send(view, 'rebuildOutline', None)
        assert_editor_source(17)

        # Clicking the other tab's Agent establishes a relation before the
        # tab activation sync runs. The sync must not clear that Agent source.
        for agent_id, editor_id in ((18, 17), (8, 7)):
            select_entry(outline, get(entry('entries_by_id', agent_id), 'identity'))
            self.assertTrue(link_state(entry('entries_by_id', agent_id), 'linked'))
            send(view, 'syncEditorSelectionToWindowID:', None, (C.c_ulonglong,), (agent_id,))
            for refresh in (False, True):
                if refresh:
                    tab_rows(0, label=b'Refreshed editor', state=3)
                selected = send(outline, 'selectedRow', C.c_long)
                self.assertGreaterEqual(selected, 0)
                self.ae(send(get(view, 'link_source_window_id'), 'unsignedLongLongValue', C.c_ulonglong), agent_id)
                self.assertTrue(link_state(entry('entries_by_id', agent_id), 'linked'))
                self.assertTrue(link_state(entry('editors_by_id', editor_id), 'related'))
                self.ae(send(editor_outline, 'selectedRow', C.c_long), -1)

        # Exercise the click -> tab activation -> pane activation path against
        # the native outline. Tab B remembers Agent B, but the click targets B2.
        from types import MethodType, SimpleNamespace
        from unittest.mock import Mock, patch

        from kitty.agents import AgentSidebarRow, AgentState
        from kitty.boss import Boss
        from kitty.tabs import Tab as TerminalTab, TabManager

        class Tab(list):
            def set_active_window(self, window, for_keep_focus=None):
                self.active_window = window
                TerminalTab.active_window_changed(self)

        manager = object.__new__(TabManager)
        first, previous, target = (SimpleNamespace(id=identity) for identity in (8, 18, 28))
        first_tab, target_tab = Tab([first]), Tab([previous, target])
        first_tab.active_window, target_tab.active_window = first, previous
        for tab in (first_tab, target_tab):
            tab.relayout = Mock()
            tab.os_window_id = 7
            tab.id = 1 if tab is first_tab else 2
            tab.mark_tab_bar_dirty = Mock()
            tab.relayout_borders = Mock()
            tab.current_layout = SimpleNamespace(only_active_window_visible=False, update_visibility=Mock())
            tab.windows = SimpleNamespace(groups=[])
            tab.tab_manager_ref = lambda: manager
            for window in tab:
                window.tabref = lambda tab=tab: tab
        manager.tabs = [first_tab, target_tab]
        manager.os_window_id = 7
        manager._active_tab_idx = 0
        manager.agent_sidebar_enabled = True
        manager.agent_sidebar_rows = (AgentSidebarRow(28, '', '', 'opencode', 'Agent B2', '', AgentState.idle, False),)
        manager.mark_tab_bar_dirty = Mock()
        manager._set_active_tab = lambda idx: setattr(manager, '_active_tab_idx', idx)
        manager.set_active_tab = lambda tab, **kw: TabManager.set_active_tab_idx(manager, manager.tabs.index(tab))

        class Controller:
            @property
            def active_tab(self):
                return manager.active_tab

        boss = Controller()
        boss.os_window_map = {7: manager}
        boss.window_id_map = {w.id: w for tab in manager for w in tab}
        boss.agent_sidebar_enabled = True
        boss.agent_sidebar_scope = lambda tm: (manager,)
        boss.refresh_agent_sidebars = lambda: tab_rows(0)
        for name in ('agents_sidebar_click', 'agents_sidebar_menu', 'set_active_window'):
            setattr(boss, name, MethodType(getattr(Boss, name), boss))

        def sync_selection(os_window_id, window_id):
            send(view, 'syncEditorSelectionToWindowID:', None, (C.c_ulonglong,), (window_id,))

        select_entry(outline, get(entry('entries_by_id', 28), 'identity'))
        with patch('kitty.tabs.get_boss', return_value=boss), patch('kitty.tabs.set_active_window'), \
                patch('kitty.boss.set_agents_sidebar_focus'), \
                patch('kitty.boss.current_focused_os_window_id', return_value=7), \
                patch('kitty.tabs.sync_agents_sidebar_editor_selection', side_effect=sync_selection):
            boss.agents_sidebar_click('7:0')
        self.assertIs(manager.active_window, target)
        self.ae(send(get(view, 'link_source_window_id'), 'unsignedLongLongValue', C.c_ulonglong), 28)
        self.assertTrue(link_state(entry('entries_by_id', 28), 'linked'))
        self.assertFalse(link_state(entry('entries_by_id', 18), 'linked'))
        self.assertTrue(link_state(entry('editors_by_id', 17), 'related'))
        selected_row = send(outline, 'selectedRow', C.c_long)
        self.ae(send(outline, 'itemAtRow:', types=(C.c_long,), args=(selected_row,)),
                get(entry('entries_by_id', 28), 'identity'))

        # Native macOS tabs are separate OS windows. Their focus callback must
        # replace a stale source even when no internal tab/pane value changes.
        target.focus_changed = Mock()
        target.clear_needs_attention = Mock()
        target.title = 'Agent B2'
        manager.tab_bar = SimpleNamespace(cancel_selection=Mock())
        sibling = SimpleNamespace(os_window_id=9)
        boss.agent_sidebar_scope = lambda tm: (manager, sibling)
        select_entry(outline, get(entry('entries_by_id', 18), 'identity'))
        with patch('kitty.boss.get_options', return_value=SimpleNamespace(draw_window_borders_for_single_window=False)), \
                patch('kitty.boss.cocoa_set_menubar_title'), \
                patch('kitty.boss.sync_agents_sidebar_editor_selection', side_effect=sync_selection) as sync:
            Boss.on_focus(boss, 7, True)
            self.ae(send(get(view, 'link_source_window_id'), 'unsignedLongLongValue', C.c_ulonglong), 28)
            self.ae([call.args for call in sync.call_args_list], [(7, 28), (9, 28)])
            Boss.on_focus(boss, 7, False)
            self.ae(send(get(view, 'link_source_window_id'), 'unsignedLongLongValue', C.c_ulonglong), 28)
            editor_window = SimpleNamespace(id=17, title='Editor B', focus_changed=Mock(), clear_needs_attention=Mock())
            target_tab.active_window = editor_window
            Boss.on_focus(boss, 7, True)
            assert_editor_source(17)
            target_tab.active_window = SimpleNamespace(id=999, title='Shell', focus_changed=Mock(), clear_needs_attention=Mock())
            Boss.on_focus(boss, 7, True)
            assert_editor_source(0)

        # A tab with no editor clears stale selection and all relation flags,
        # although editor rows from the other tabs are still present.
        tab_rows(0)
        send(view, 'syncEditorSelectionToWindowID:', None, (C.c_ulonglong,), (0,))
        assert_editor_source(0)
        tab_rows(0, label=b'Still visible editor B', state=2)
        assert_editor_source(0)
        select_entry(editor_outline, entry('editors_by_id', 7))
        send(view, 'syncEditorSelectionToWindowID:', None, (C.c_ulonglong,), (999,))
        assert_editor_source(0)
        rows()
        agent, editor = entry('entries_by_id', 7), entry('editors_by_id', 7)
        agent_identity = get(agent, 'identity')

        cell = send(outline, 'viewAtColumn:row:makeIfNecessary:', types=(C.c_long, C.c_long, C.c_bool), args=(0, 0, True))
        rows(label=b'Updated editor')
        self.ae(entry('entries_by_id', 7), agent)
        self.ae(entry('editors_by_id', 7), editor)
        if cell:
            self.ae(send(outline, 'viewAtColumn:row:makeIfNecessary:', types=(C.c_long, C.c_long, C.c_bool), args=(0, 0, True)), cell)
        self.assertFalse(send(view, 'workingSpinnerColorForNode:', types=(ptr,), args=(editor,)))
        rows(0, 0)
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
        send(view, 'updateSectionHeaders', None, ())

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
        self.assertAlmostEqual(frame('agents_header').origin.y, 0)
        divider_hit_view = get(view, 'divider_hit_view')
        self.assertFalse(send(divider_hit_view, 'isHidden', C.c_bool))
        self.assertAlmostEqual(frame('divider_hit_view').size.height, 8)
        send(divider_hit_view, 'cursorUpdate:', None, (ptr,), (None,))
        self.ae(send(cls('NSCursor'), 'currentCursor'), send(cls('NSCursor'), 'resizeUpDownCursor'))
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
                      types=(Rect, C.c_ulong, C.c_ulong, C.c_bool), args=(Rect(Point(0, 0), Size(260, 500)), 0, 2, True))

        def release_window():
            send(window, 'setContentView:', None, (ptr,), (None,))
            send(window, 'release', None)

        self.addCleanup(release_window)
        send(window, 'setContentView:', None, (ptr,), (view,))
        divider_rect = frame('divider_hit_view')
        divider_point = send(view, 'convertPoint:toView:', Point, (Point, ptr),
                              (Point(20, divider_rect.origin.y + divider_rect.size.height / 2), send(view, 'superview')))
        hit = send(view, 'hitTest:', types=(Point,), args=(divider_point,))
        self.ae(hit, divider_hit_view)
        send(view, 'focusSidebar', None)
        editor_outline = get(view, 'editor_outline')
        self.ae(send(window, 'firstResponder'), editor_outline)
        self.ae(send(view, 'navigationOutline'), editor_outline)
        selected_row = send(editor_outline, 'selectedRow', C.c_long)
        self.assertGreaterEqual(selected_row, 0)
        self.ae(send(editor_outline, 'itemAtRow:', types=(C.c_long,), args=(selected_row,)), active_editor)
        # The active pane context wins even when an active Agent is also visible.
        rows(8, 7)
        active_agent = entry('entries_by_id', 8)
        active_editor = entry('editors_by_id', 7)
        set_value(active_agent, 'active', 1)
        set_value(active_editor, 'active', 1)
        send(view, 'kittySidebarCancelNavigation', None)
        send(view, 'focusSidebar', None)
        self.ae(send(window, 'firstResponder'), editor_outline)
        selected_row = send(editor_outline, 'selectedRow', C.c_long)
        self.assertGreaterEqual(selected_row, 0)
        self.ae(send(editor_outline, 'itemAtRow:', types=(C.c_long,), args=(selected_row,)), active_editor)


        # Keyboard navigation previews rows without committing a new relation
        # source. Returning focus to the terminal clears only that preview.
        tab_rows(0)
        send(view, 'syncEditorSelectionToWindowID:', None, (C.c_ulonglong,), (18,))
        send(view, 'focusSidebar', None)

        def press(characters):
            event = send(cls('NSEvent'), 'keyEventWithType:location:modifierFlags:timestamp:windowNumber:context:characters:charactersIgnoringModifiers:isARepeat:keyCode:',
                         types=(C.c_ulong, Point, C.c_ulong, C.c_double, C.c_long, ptr, ptr, ptr, C.c_bool, C.c_ushort),
                         args=(10, Point(0, 0), 0, 0, 0, None, string(characters), string(characters), False, 0))
            self.assertTrue(send(view, 'kittySidebarHandleKeyEvent:', C.c_bool, (ptr,), (event,)))

        def assert_agent_relation(identity):
            self.ae(send(get(view, 'link_source_window_id'), 'unsignedLongLongValue', C.c_ulonglong), identity)
            for agent_id in (8, 18, 28):
                self.ae(link_state(entry('entries_by_id', agent_id), 'linked'), agent_id == identity)
            self.assertTrue(link_state(entry('editors_by_id', 17), 'related'))

        self.ae(send(view, 'navigationOutline'), outline)
        press('\uf701')  # Down: Agent B -> Agent B2, without Enter.
        self.ae(send(view, 'selectedNode'), get(entry('entries_by_id', 28), 'identity'))
        assert_agent_relation(18)
        tab_rows(0, label=b'Refreshed while navigating')
        assert_agent_relation(18)
        send(view, 'rebuildOutline', None)
        assert_agent_relation(18)
        press('\t')
        assert_agent_relation(18)
        press('\t')
        press('\uf700')  # Up: back to Agent B.
        assert_agent_relation(18)
        press('\uf701')
        # AppKit transfers first responder when the terminal is clicked.
        from kitty.constants import glfw_path

        glfw_module = C.CDLL(glfw_path('cocoa'), mode=C.RTLD_GLOBAL)
        terminal = send(send(cls('GLFWContentView'), 'alloc'), 'initWithGlfwWindow:', types=(ptr,), args=(None,))
        self.addCleanup(lambda glfw_module=glfw_module: send(terminal, 'release', None))
        send(terminal, 'setFrame:', None, (Rect,), (Rect(Point(260, 0), Size(200, 500)),))
        container = send(send(cls('NSView'), 'alloc'), 'initWithFrame:', types=(Rect,),
                         args=(Rect(Point(0, 0), Size(460, 500)),))
        self.addCleanup(lambda: send(container, 'release', None))
        send(view, 'retain')
        send(window, 'setContentView:', None, (ptr,), (container,))
        send(container, 'addSubview:', None, (ptr,), (view,))
        send(view, 'release', None)
        send(container, 'addSubview:', None, (ptr,), (terminal,))
        send(view, 'focusSidebar', None)
        send(terminal, 'mouseDown:', None, (ptr,), (None,))
        self.ae(send(window, 'firstResponder'), terminal)
        self.ae(send(outline, 'selectedRow', C.c_long), -1)
        self.ae(send(editor_outline, 'selectedRow', C.c_long), -1)
        self.assertFalse(send(view, 'kittySidebarHasKeyboardFocus', C.c_bool))
        assert_agent_relation(18)
        # Active-pane synchronization must not recreate the keyboard preview.
        send(view, 'syncEditorSelectionToWindowID:', None, (C.c_ulonglong,), (28,))
        self.ae(send(outline, 'selectedRow', C.c_long), -1)
        self.ae(send(editor_outline, 'selectedRow', C.c_long), -1)
        assert_agent_relation(28)
        send(view, 'syncEditorSelectionToWindowID:', None, (C.c_ulonglong,), (18,))
        send(view, 'focusSidebar', None)
        assert_agent_relation(18)
        press('\uf701')
        # No GLFW event loop runs in this harness; capture the outbound command.
        objc.objc_allocateClassPair.argtypes = [ptr, C.c_char_p, C.c_size_t]
        objc.objc_allocateClassPair.restype = ptr
        objc.objc_registerClassPair.argtypes = [ptr]
        objc.class_addMethod.argtypes = [ptr, ptr, ptr, C.c_char_p]
        objc.class_addMethod.restype = C.c_bool
        objc.object_setClass.argtypes = [ptr, ptr]
        objc.object_setClass.restype = ptr
        commands = []

        def capture_command(obj, selector, command, window_id):
            commands.append((text(command), window_id))
            send(obj, 'kittySidebarCancelNavigation', None)

        callback = C.CFUNCTYPE(None, ptr, ptr, ptr, C.c_ulonglong)(capture_command)
        command_class = objc.objc_allocateClassPair(cls('KittyAgentsSidebarView'), f'KittySidebarCommandTest_{view:x}'.encode(), 0)
        self.assertTrue(command_class)
        self.assertTrue(objc.class_addMethod(command_class, objc.sel_registerName(b'sendCommand:windowID:'),
                                            C.cast(callback, ptr), b'v@:@Q'))
        objc.objc_registerClassPair(command_class)
        original_class = objc.object_setClass(view, command_class)
        self.addCleanup(lambda callback=callback: objc.object_setClass(view, original_class))
        press('\r')
        self.ae(commands, [('focus', 28)])
        assert_agent_relation(28)
        self.ae(send(outline, 'selectedRow', C.c_long), -1)
        send(view, 'focusSidebar', None)
        press('\uf700')
        press('\x1b')
        assert_agent_relation(28)
        self.ae(send(outline, 'selectedRow', C.c_long), -1)
