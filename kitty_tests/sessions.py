#!/usr/bin/env python
# License: GPL v3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

import json
import os
import shlex
import shutil
import subprocess
import tempfile

from kitty.constants import unserialize_launch_flag
from kitty.fast_data_types import Screen
from kitty.os_window_size import WindowSize, WindowSizes, initial_window_size_func
from kitty.session import get_os_window_sizing_data, parse_session, save_as_session

from .base import BaseTest, parse_bytes


class TestSessions(BaseTest):
    def test_window_font_size_override_session_state(self) -> None:
        opts = self.set_options()
        encoded = unserialize_launch_flag + '{"id": 7, "font_size_override": 15.5}'
        (session,) = parse_session(f'launch {shlex.join((encoded,))}', opts)
        self.ae(session.tabs[0].windows[0].font_size_override, 15.5)

        # Session files written by older kitty versions have no override field.
        old_encoded = unserialize_launch_flag + '{"id": 8}'
        (session,) = parse_session(f'launch {shlex.join((old_encoded,))}', opts)
        self.ae(session.tabs[0].windows[0].font_size_override, None)

    def test_snapshot_preserves_os_window_docks_and_pane_fonts(self) -> None:
        from kitty.boss import Boss
        from kitty.logout_session import save_session_snapshot
        from kitty.tabs import Tab, TabManager
        from kitty.types import OverlayType, parse_dock_spec
        from kitty.window import EdgeWidths, Window
        from kitty.window_list import WindowList

        opts = self.set_options()
        tm = object.__new__(TabManager)
        tm.os_window_id, tm.wm_class, tm.wm_name = 1, '', ''
        tm.tabs, tm.os_window_docks = [], []
        tm._active_tab_idx = 1
        tm.serialize_geometry_as_session = lambda: []
        windows = []
        for tab_id in (1, 2):
            tab = object.__new__(Tab)
            tab.id, tab.os_window_id, tab.sidebar_id = tab_id, 1, f'saved-{tab_id}'
            tab.name, tab.enabled_layouts, tab._current_layout_name = f'tab-{tab_id}', ['tall'], 'tall'
            tab.serialize_layout_state_for_session = lambda: {}
            tab.windows = WindowList(tab, tm.os_window_docks)
            tab.windows.hosts_os_window_docks = tab_id == 2
            tm.tabs.append(tab)
            for wid in ((1,) if tab_id == 1 else (2, 3)):
                window = SimpleNamespace(
                    id=wid, os_window_id=1, tab_id=tab_id, overlay_type=OverlayType.transient,
                    actions_on_close=[], actions_on_focus_change=[], actions_on_removal=[],
                    allow_remote_control=False, remote_control_passwords=None, creation_spec=None,
                    user_vars={}, padding=EdgeWidths(), margin=EdgeWidths(), override_title=None,
                    font_size_override=15.5 if wid == 3 else None, last_focused_at=float(wid),
                    cwd_for_serialization='/tmp', agent_for_resume=lambda: '',
                    screen=self.create_screen(cols=20, lines=2),
                )
                window.as_launch_command = partial(Window.as_launch_command, window)
                windows.append(window)
                tab.windows.add_window(
                    window, make_active=False,
                    dock=parse_dock_spec('right', '25%') if wid == 3 else None,
                    os_window_dock=wid == 3,
                )
        parse_bytes(windows[-1].screen, b'docked history')
        boss = SimpleNamespace(
            os_window_map={1: tm}, native_tabs_enabled=lambda: True, all_windows=windows,
            agent_sidebar_enabled=True, agent_sidebar_cols=36, agent_sidebar_collapsed_groups={'tab:saved-2'},
        )
        boss.serialize_state_as_session = partial(Boss.serialize_state_as_session, boss)
        captured = {}

        def save(base, pid, session, scrollback, process_state, key_store):
            captured.update(session=session, scrollback=scrollback)
            return os.path.join(base, 'snapshot')

        with (
            TemporaryDirectory() as directory,
            patch('kitty.boss.native_tab_group_order', return_value=(1,)),
            patch('kitty.boss.current_focused_os_window_id', return_value=1),
            patch('kitty.boss.last_focused_os_window_id', return_value=1),
            patch('kitty.tabs.get_boss', return_value=boss),
            patch('kitty.window.get_window_logo_settings_if_not_default', return_value=None),
            patch('kitty.logout_session.snapshots_dir', return_value=os.path.join(directory, 'snapshots')),
            patch('kitty.logout_session.get_key_store', return_value=None),
            patch('kitty.logout_session.save_snapshot', side_effect=save),
            patch('kitty.logout_session.lock_snapshot_for_saver'),
            patch('kitty.logout_session.last_saved_session', ''),
        ):
            self.assertTrue(save_session_snapshot(boss))
        (session,) = parse_session(captured['session'], opts)
        self.ae((session.os_window_tab_group, session.focus_os_window, session.focus_tab_spec), ('1', True, '1'))
        self.ae((session.agent_sidebar_enabled, session.agent_sidebar_cols, session.agent_sidebar_collapsed_groups), (True, 36, ('tab:saved-2',)))
        self.ae([tab.sidebar_id for tab in session.tabs], ['saved-1', 'saved-2'])
        saved = [window for tab in session.tabs for window in tab.windows]
        self.ae(sorted(window.serialized_id for window in saved), [1, 2, 3])
        dock = next(window for window in saved if window.serialized_id == 3)
        self.ae((dock.launch_spec.opts.type, dock.launch_spec.opts.dock_edge, dock.launch_spec.opts.dock_size), ('os-window-dock', 'right', '25%'))
        self.ae(dock.font_size_override, 15.5)
        self.assertIn(b'docked history', captured['scrollback'][3])

    def test_restored_window_user_vars(self) -> None:
        opts = self.set_options()

        def variables(metadata: dict[str, object] | None) -> list[str]:
            args = ['launch']
            if metadata is not None:
                args.append(unserialize_launch_flag + json.dumps(metadata))
            args.extend(('--var=in_editor=1', '--var=project=terminal', '--var=KITTY_AGENT_SESSION=abc-123'))
            (session,) = parse_session(shlex.join(args), opts)
            return session.tabs[0].windows[0].launch_spec.opts.var

        # Old snapshots cannot distinguish program state from explicit launch labels.
        self.ae(variables({'id': 7}), ['KITTY_AGENT_SESSION=abc-123'])
        # New snapshots retain explicit labels, not runtime editor state.
        self.ae(variables({'id': 7, 'launch_vars': ['project']}), ['project=terminal', 'KITTY_AGENT_SESSION=abc-123'])
        # A hand-authored session can still explicitly enter an application-specific mode.
        self.ae(variables(None), ['in_editor=1', 'project=terminal', 'KITTY_AGENT_SESSION=abc-123'])

    def test_default_session_window_metadata(self) -> None:
        from kitty.session import create_sessions

        (session,) = create_sessions(self.set_options())
        self.ae(
            (
                session.os_window_size,
                session.os_window_position,
                session.os_window_class,
                session.os_window_name,
                session.os_window_title,
                session.os_window_state,
            ),
            (None, None, None, None, None, None),
        )

    def test_session_size_overrides_remembered_size(self) -> None:
        opts = self.set_options({'remember_window_size': True})
        with_size, without_size = parse_session('os_window_size 1180 760\nlaunch\nnew_os_window\nlaunch', opts)
        remembered = {'window-size': (900, 600)}
        sd = get_os_window_sizing_data(opts, with_size)
        self.ae((sd.remember_window_size, sd.initial_window_sizes), (False, WindowSizes(WindowSize(1180, 'px'), WindowSize(760, 'px'))))
        for session in (without_size, None):
            sd = get_os_window_sizing_data(opts, session)
            self.assertTrue(sd.remember_window_size)
            self.ae(initial_window_size_func(sd, remembered)(10, 20, 72, 72, 1, 1), (900, 600))

    def test_session_os_window_geometry(self) -> None:
        from kitty.fast_data_types import WINDOW_FULLSCREEN, WINDOW_MAXIMIZED, WINDOW_NORMAL
        from kitty.tabs import os_window_geometry_as_session

        opts = self.set_options()
        first, second = parse_session('os_window_position -1200x80\nlaunch\nnew_os_window\nlaunch', opts)
        self.ae((first.os_window_position, second.os_window_position), ((-1200, 80), None))
        with self.assertRaises(ValueError):
            tuple(parse_session('os_window_position 100\nlaunch', opts))

        g = {'x': -1200, 'y': 80, 'width': 1180, 'height': 760, 'state': WINDOW_MAXIMIZED}
        lines = os_window_geometry_as_session(g)
        self.ae(lines, ['os_window_size 1180 760', 'os_window_position -1200x80', 'os_window_state maximized'])
        self.ae(os_window_geometry_as_session(g, include_position=False), [lines[0], lines[2]])
        (s,) = parse_session('\n'.join(lines + ['launch']), opts)
        size = WindowSizes(WindowSize(1180, 'px'), WindowSize(760, 'px'))
        self.ae((s.os_window_size, s.os_window_position, s.os_window_state), (size, (-1200, 80), 'maximized'))
        for state, name in ((WINDOW_FULLSCREEN, 'fullscreen'), (WINDOW_NORMAL, 'normal')):
            self.ae(os_window_geometry_as_session({**g, 'state': state})[-1], f'os_window_state {name}')

    def test_session_agent_sidebar_state(self) -> None:
        opts = self.set_options()
        raw = '\n'.join(
            (
                'agent_sidebar {"enabled":true,"width_cols":42,"collapsed_groups":["agent:claude","tab:tab-7"]}',
                'new_tab first',
                'tab_id tab-7',
                'launch',
                'new_tab second',
                'tab_id tab-8',
                'launch',
            )
        )
        (session,) = parse_session(raw, opts)
        self.ae((session.agent_sidebar_enabled, session.agent_sidebar_cols, session.agent_sidebar_collapsed_groups), (True, 42, ('agent:claude', 'tab:tab-7')))
        self.ae([tab.sidebar_id for tab in session.tabs], ['tab-7', 'tab-8'])

    def test_native_tab_groups_in_saved_session(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        from kitty.boss import Boss
        from kitty.tabs import TabManager

        opts = self.set_options()
        layout_state = {'pairs': [11, 12], 'bias': 0.37}

        def manager(wid: int, empty: bool = False) -> SimpleNamespace:
            # Use the real TabManager serializer, including its empty-window behavior.
            tab = SimpleNamespace(
                serialize_state_as_session=Mock(
                    return_value=[]
                    if empty
                    else [
                        f'new_tab window-{wid}',
                        'layout splits',
                        f'set_layout_state {__import__("json").dumps(layout_state)}',
                        f'launch --title=window-{wid} /bin/sh',
                        'launch /bin/sh',
                        'focus',
                    ]
                )
            )
            tm = SimpleNamespace(
                tabs=[tab],
                active_tab=tab,
                wm_class='',
                wm_name='',
                os_window_docks=[],
                serialize_geometry_as_session=lambda: [
                    'os_window_size 1180 760',
                    'os_window_position 100x80',
                ],
            )
            tm.serialize_state_as_session = lambda *a, **kw: TabManager.serialize_state_as_session(tm, *a, **kw)
            return tm

        # Creation order, native tab order and focus order all differ. All windows have identical geometry.
        order = {1: (3, 1, 2), 2: (3, 1, 2), 3: (3, 1, 2), 4: (5, 4), 5: (5, 4), 6: None, 7: (7,)}
        boss = SimpleNamespace(os_window_map={wid: manager(wid) for wid in range(1, 8)}, native_tabs_enabled=lambda: True)
        with (
            patch('kitty.boss.native_tab_group_order', side_effect=order.get),
            patch('kitty.boss.current_focused_os_window_id', return_value=1),
            patch('kitty.boss.last_focused_os_window_id', return_value=4),
        ):
            raw = '\n'.join(Boss.serialize_state_as_session(boss))
            sessions = tuple(parse_session(raw, opts))
            self.ae([s.tabs[0].name for s in sessions], [f'window-{wid}' for wid in (3, 1, 2, 5, 4, 6, 7)])
            self.ae([s.os_window_tab_group for s in sessions], ['1', '1', '1', '2', '2', '3', '4'])
            self.ae([s.focus_os_window for s in sessions], [False, True, False, False, False, False, False])
            for s in sessions:
                self.ae(s.tabs[0].layout, 'splits')
                self.ae(s.tabs[0].layout_state, layout_state)
                self.ae(len(s.tabs[0].windows), 2)
                self.ae(s.tabs[0].active_window_idx, 1)

            # Filtering out the first native member and an entire group must not produce an empty leading OS window.
            for wid in (3, 4, 5):
                boss.os_window_map[wid] = manager(wid, empty=True)
            filtered = tuple(parse_session('\n'.join(Boss.serialize_state_as_session(boss)), opts))
            self.ae([s.tabs[0].name for s in filtered], [f'window-{wid}' for wid in (1, 2, 6, 7)])
            self.ae([s.os_window_tab_group for s in filtered], ['1', '1', '3', '4'])
            self.assertTrue(filtered[0].focus_os_window)

    def test_save_as_session_first_os_window(self) -> None:
        # Only the first OS window that has something to save is loaded into the
        # current OS window, the rest go into new OS windows
        from kitty.boss import Boss
        from kitty.session import default_save_as_session_opts

        def serialize(focused_os_window_id: int, *os_windows: list[str]) -> list[str]:
            def tab_manager(lines: list[str]) -> SimpleNamespace:
                def serialize_state_as_session(session_path: str, matched_windows: frozenset[object] | None, is_first: bool, ser_opts: object) -> list[str]:
                    return (lines if is_first else ['', '', 'new_os_window'] + lines) if lines else []

                return SimpleNamespace(serialize_state_as_session=serialize_state_as_session)

            boss = SimpleNamespace(os_window_map={i: tab_manager(lines) for i, lines in enumerate(os_windows, start=1)}, native_tabs_enabled=lambda: False)
            with (
                patch('kitty.boss.current_focused_os_window_id', return_value=focused_os_window_id),
                patch('kitty.boss.last_focused_os_window_id', return_value=focused_os_window_id),
            ):
                return list(Boss.serialize_state_as_session(cast(Boss, boss), '', default_save_as_session_opts()))

        tab = ['', 'new_tab', 'launch']
        # The focused OS window is serialized last
        lines = serialize(2, [], tab)
        self.ae(lines, tab + ['focus_os_window'])
        self.ae(len(list(parse_session('\n'.join(lines), self.set_options()))), 1)
        self.ae(serialize(3, [], tab, tab), tab + ['', '', 'new_os_window'] + tab + ['focus_os_window'])
        self.ae(serialize(1, tab, []), tab + ['focus_os_window'])

    def test_save_as_session_focus_tab(self) -> None:
        from kitty.tabs import index_of_tab_to_focus_in_session as f

        # The active tab, as an index among the saved tabs, when it is saved
        self.ae(f(1, [5, 1, 2]), 1)
        # Otherwise the saved tab with the most recently focused saved window
        self.ae(f(-1, [1, 5, 2]), 1)
        self.ae(f(-1, [1, 5, 7]), 2)
        self.ae(f(-1, [0, 0]), 0)
        lines = ['', 'new_tab a', 'launch', '', 'new_tab b', 'launch', '', 'focus_tab 1']
        (s,) = parse_session('\n'.join(lines), self.set_options())
        self.ae((len(s.tabs), s.focus_tab_spec), (2, '1'))

    def test_save_as_session_nothing_to_save(self) -> None:
        boss = Mock(active_session='work')
        serialized: tuple[str, ...] = ('launch',)
        matched: tuple[str, ...] = ()
        boss.serialize_state_as_session.side_effect = lambda *a: iter(serialized)
        boss.match_windows.side_effect = lambda *a: iter(matched)

        def save(*args: str) -> None:
            for m in (boss.show_error, boss.match_windows, boss.get_save_filepath):
                m.reset_mock()
            save_as_session(boss, args)

        def assert_error(*fragments: str) -> None:
            self.ae(boss.show_error.call_count, 1)
            title, msg = boss.show_error.call_args.args
            self.ae(title, 'No windows to save')
            for frag in fragments:
                self.assertIn(frag, msg)

        match = '--match=title:^editor$'
        with TemporaryDirectory() as tmp:
            existing = Path(tmp, 'work.kitty-session')
            new = Path(tmp, 'new', 'work.kitty-session')
            with patch.dict('kitty.session.seen_session_paths', work=str(existing)):
                with self.subTest(destination='active session'):
                    save('--save-only', match, '.')
                    self.ae(existing.read_bytes(), b'launch')
                    boss.show_error.assert_not_called()
                with self.subTest(destination='prompt for path, windows closed before answer'):
                    save(match)
                    boss.get_save_filepath.assert_called_once()
                    callback = boss.get_save_filepath.call_args.args[1]
                    serialized = ()
                    callback(str(new))
                    self.assertFalse(new.parent.exists())
                    assert_error('No windows match: title:^editor$')
                with self.subTest(destination='active session, no windows match'):
                    save(match, '.')
                    self.ae(existing.read_bytes(), b'launch')
                    assert_error('No windows match: title:^editor$')
                    boss.match_windows.assert_called_with('title:^editor$')
                matched = ('window',)
                with self.subTest(destination='active session, matched windows not saveable'):
                    save(match, '.')
                    self.ae(existing.read_bytes(), b'launch')
                    assert_error('None of the matching windows')
                with self.subTest(destination='active session, no --match'):
                    save('--save-only', '.')
                    self.ae(existing.read_bytes(), b'launch')
                    assert_error('None of the windows')
                    boss.match_windows.assert_not_called()
                with self.subTest(destination='new file'):
                    save('--save-only', match, str(new))
                    self.assertFalse(new.parent.exists())
                    assert_error('None of the matching windows')
                with self.subTest(destination='prompt for path, nothing to save'):
                    save(match)
                    boss.get_save_filepath.assert_not_called()
                    assert_error('None of the matching windows')
        boss.edit_file.assert_not_called()

    def test_scrollback_for_restore(self) -> None:
        from kitty.logout_session import sanitize_restored_output, scrollback_for_restore
        from kitty.window import as_text

        def restore(data: bytes, cols: int = 10, lines: int = 4, scrollback: int = 20) -> Screen:
            s = self.create_screen(cols=cols, lines=lines, scrollback=scrollback)
            s.restore_scrollback(sanitize_restored_output(data.decode()).encode())
            return s

        def text(s: Screen) -> list[str]:
            return as_text(s, add_history=True).rstrip('\n').splitlines()

        src = self.create_screen(cols=10, lines=4, scrollback=20)
        parse_bytes(
            src, b''.join(b'line %d\r\n' % i for i in range(8)) + b'\x1b[31mred\x1b[m 0123456789abcdef\r\n\x1b]8;;https://x.org\x1b\\link\x1b]8;;\x1b\\'
        )
        expected, expected_ansi = text(src), as_text(src, as_ansi=True, add_history=True).rstrip('\n')
        # Output on the alternate screen, such as from a full screen program, is not restored
        parse_bytes(src, b'\x1b[?1049hfull screen program')
        data = scrollback_for_restore(src)
        self.assertNotIn(b'full screen program', data)
        self.assertFalse(data.endswith(b'\r\n'))
        same_width = restore(data)
        self.ae(text(same_width), expected)
        self.ae(as_text(same_width, as_ansi=True, add_history=True).rstrip('\n'), expected_ansi)
        # Wrapped lines are re-wrapped to the width of the new screen
        wider = restore(data, cols=30)
        self.ae(text(wider)[-2:], ['red 0123456789abcdef', 'link'])
        self.assertTrue(any(url.endswith('https://x.org') for url, hyperlink_id in wider.hyperlinks_as_set()))

        # The scrollback pager history is not saved, so at most scrollback + lines lines are restored
        src = self.create_screen(cols=10, lines=4, scrollback=5)
        parse_bytes(src, b'\r\n'.join(b'%d' % i for i in range(30)))
        self.ae(text(restore(scrollback_for_restore(src), scrollback=50)), [str(i) for i in range(21, 30)])

        # The shell prompt the cursor is at is not restored, since the restored shell shows a new one
        def restored_lines(data: bytes, lines: int = 6) -> list[str]:
            src = self.create_screen(cols=20, lines=lines, scrollback=50)
            parse_bytes(src, data)
            return text(restore(scrollback_for_restore(src), cols=20, lines=lines, scrollback=50))

        prompt, output = b'\x1b]133;A\x07', b'\x1b]133;C\x07'
        self.ae(restored_lines(prompt + b'$ ls\r\n' + output + b'a\r\nb\r\n' + prompt + b'$ partial'), ['$ ls', 'a', 'b'])
        two_line_prompt = prompt + b'~/project on main\r\n$ '
        self.ae(restored_lines(two_line_prompt + b'ls\r\n' + output + b'a\r\n' + two_line_prompt), ['~/project on main', '$ ls', 'a'])
        many = b''.join(b'%d\r\n' % i for i in range(10))
        self.ae(restored_lines(prompt + b'$ seq\r\n' + output + many + prompt + b'$ ', lines=4), ['$ seq'] + [str(i) for i in range(10)])
        # A running command is restored with all its output
        self.ae(restored_lines(prompt + b'$ tail -f log\r\n' + output + b'a\r\nb'), ['$ tail -f log', 'a', 'b'])

        # Shell integration marks are not restored, so restored output is not treated as the output of a command
        src = self.create_screen(cols=20, lines=6, scrollback=50)
        parse_bytes(src, prompt + b'$ ls\r\n' + output + b'a\r\n' + prompt + b'$ ')
        data = scrollback_for_restore(src)
        self.assertIn(b'\x1b]133;C', data)
        self.assertNotIn('\x1b]133;', sanitize_restored_output(data.decode()))

        # Output larger than the parser buffer
        big = self.create_screen(cols=80, lines=4, scrollback=20)
        big.restore_scrollback(b''.join(b'%05d %s\r\n' % (i, b'x' * 70) for i in range(16000)))
        self.ae(text(big)[-1], '15999 ' + 'x' * 70)

        # Restoring scrollback while the child is writing to the screen raises instead of crashing
        s = self.create_screen()
        s.test_create_write_buffer()
        with self.assertRaises(RuntimeError):
            s.restore_scrollback(b'x')

    def test_sanitize_restored_output(self) -> None:
        from kitty.logout_session import sanitize_restored_output as s

        sgr, link, link_end, sized = '\x1b[1;38:2:1:2:3m', '\x1b]8;id=1;https://x.org\x1b\\', '\x1b]8;;\x07', '\x1b]66;s=2;big\x1b\\'
        text = f'{sgr}bold\x1b[m {link}link{link_end} {sized}\ttab\r\n'
        self.ae(s(text), text)
        for unsafe, expected in (
            ('\x1b]52;c;aGVsbG8=\x07', 'ab'),
            ('\x1b]133;A\x1b\\', 'ab'),
            ('\x1b]99;;notification\x1b\\', 'ab'),
            ('\x1bP@kitty-cmd{"cmd":"ls"}\x1b\\', 'ab'),
            ('\x1b_Gf=100;AAAA\x1b\\', 'ab'),
            ('\x1b[2J', 'ab'),
            ('\x1b[?1049h', 'ab'),
            ('\x1bc', 'ab'),
            ('\x07\x08\x0c', 'ab'),
            ('\x1b', 'a'),  # ESC followed by a character is an escape code
            ('\x9b2J', 'a2Jb'),  # C1 control codes are removed, leaving harmless text
            ('\x1b]0;title', 'a'),  # an unterminated OSC consumes the rest of the text
        ):
            with self.subTest(unsafe=unsafe):
                self.ae(s(f'a{unsafe}b'), expected)

    def test_session_snapshot_app_isolation(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch

        from kitty import fast_data_types as fdt
        from kitty import logout_session as ls
        from kitty import session_keys as sk

        from .crypto import is_rlimit_memlock_too_low

        if is_rlimit_memlock_too_low():
            self.skipTest('RLIMIT_MEMLOCK is too low')

        self.set_options({'persist_session': 'always'})
        keys = {}

        def store(service, account, label, key):
            keys[service, account] = key

        def read(service, account):
            return keys.get((service, account))

        def delete(service, account):
            keys.pop((service, account), None)

        def list_ids(service):
            return [account for svc, account in keys if svc == service]

        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(ls, 'cache_dir', return_value=root),
            patch.object(sk, 'key_storage_root', return_value=os.path.join(os.path.realpath(root), 'keys')),
            patch.object(ls, 'is_macos', True),
            patch.object(ls, 'process_state', ls.ProcessState((), root, {}, 0)),
            patch.object(ls, 'last_saved_session', ''),
            patch.object(ls, 'saver_lock', None),
            patch.object(fdt, 'cocoa_keychain_store_secret', store, create=True),
            patch.object(fdt, 'cocoa_keychain_read_secret', read, create=True),
            patch.object(fdt, 'cocoa_keychain_delete', delete, create=True),
            patch.object(fdt, 'cocoa_keychain_list_accounts', list_ids, create=True),
        ):
            app_a = os.path.join(root, 'Applications', 'kitty.app', 'Contents', 'MacOS', 'kitty')
            app_b = os.path.join(root, 'Desktop', 'kitty.app', 'Contents', 'MacOS', 'kitty')
            legacy = os.path.join(root, 'saved-at-logout', '123')
            os.makedirs(legacy)
            for exe in (app_a, app_b):
                os.makedirs(os.path.dirname(exe))
                with open(exe, 'wb') as f:
                    f.write(b'old build')
            alias = os.path.join(root, 'kitty-alias.app')
            os.symlink(os.path.join(root, 'Applications', 'kitty.app'), alias)

            def save(cwd):
                boss = SimpleNamespace(serialize_state_as_session=lambda *args: [f'launch --cwd={cwd}'])
                try:
                    return ls.save_session_snapshot(boss, with_scrollback=False)
                finally:
                    ls.release_saver_lock()

            # Old snapshots use the shared keychain service. They are considered only
            # while this installation has no snapshot directory, not when it is empty.
            legacy_store = SimpleNamespace(
                store=lambda account, key: store(ls.KEYCHAIN_SERVICE, account, '', key),
                read=lambda account: read(ls.KEYCHAIN_SERVICE, account),
                delete=lambda account: delete(ls.KEYCHAIN_SERVICE, account),
                list_ids=lambda: list_ids(ls.KEYCHAIN_SERVICE),
            )
            legacy_base = os.path.dirname(legacy)
            shutil.rmtree(legacy)
            state = ls.ProcessState((), root, {}, 0)
            old_path = ls.save_snapshot(legacy_base, 11, 'launch --cwd=/legacy', {}, state, legacy_store)
            other_path = ls.save_snapshot(legacy_base, 12, 'launch --cwd=/legacy-other', {}, state._replace(argv=('other',)), legacy_store)
            with patch.object(ls, 'kitty_exe', return_value=app_a):
                new_base = ls.snapshots_dir()
                os.makedirs(new_base)
                self.assertIsNone(ls.restore_snapshots(self.set_options()))
                self.assertTrue(os.path.isdir(old_path))
                os.rmdir(new_base)
                restored = ls.restore_snapshots(self.set_options())
                assert restored is not None
                self.ae(restored.sessions[0].tabs[0].windows[0].launch_spec.opts.cwd, '/legacy-other')
                ls.finish_restore(restored.path)
                self.assertTrue(os.path.isdir(other_path))
                self.assertTrue(os.path.isdir(old_path))
                self.ae(len(keys), 1)  # Only the validated legacy snapshot has migrated out of the Keychain.
                self.assertIsNotNone(ls.get_key_store(legacy_base).read(ls.read_key_id(other_path)))
                ls.remove_snapshot(old_path, ls.get_key_store(legacy_base))
                ls.remove_snapshot(other_path, ls.get_key_store(legacy_base))
            os.makedirs(legacy)

            with patch.object(ls, 'kitty_exe', return_value=app_a):
                path_a = save('/app-a')
                key_a = ls.read_key_id(path_a)
                store_a = ls.get_key_store()
                assert store_a is not None
            # An alias and an in-place binary upgrade must still use A's snapshot.
            with open(app_a, 'wb') as f:
                f.write(b'new build')
            with patch.object(ls, 'kitty_exe', return_value=os.path.join(alias, 'Contents', 'MacOS', 'kitty')):
                alias_path = save('/app-a')
                self.ae(os.path.dirname(alias_path), os.path.dirname(path_a))
                self.assertNotEqual(alias_path, path_a)
                path_a = alias_path
                key_a = ls.read_key_id(path_a)
            with patch.object(ls, 'kitty_exe', return_value=app_b):
                path_b = save('/app-b')
                self.assertNotEqual(path_a, path_b)
                self.assertTrue(os.path.isdir(path_a))
                # Orphan cleanup and restoring B must leave A's encryption key intact.
                restored = ls.restore_snapshots(self.set_options())
                assert restored is not None
                self.ae(restored.sessions[0].tabs[0].windows[0].launch_spec.opts.cwd, '/app-b')
                ls.finish_restore(restored.path)
                self.assertIsNotNone(store_a.read(key_a))
                save('/app-b-again')
                ls.remove_all_snapshots()
                self.assertTrue(os.path.isdir(path_a))
                self.assertIsNotNone(store_a.read(key_a))

            with patch.object(ls, 'kitty_exe', return_value=app_a):
                # The same installation still restores its own snapshot after B's cleanup.
                restored = ls.restore_snapshots(self.set_options())
                assert restored is not None
                self.ae(restored.sessions[0].tabs[0].windows[0].launch_spec.opts.cwd, '/app-a')
                ls.finish_restore(restored.path)
                self.assertIsNotNone(store_a.read(key_a))
                ls.remove_all_snapshots()
                self.ae(keys, {})
            self.assertTrue(os.path.isdir(legacy))

    def test_logout_snapshots(self) -> None:
        from .crypto import is_rlimit_memlock_too_low

        if is_rlimit_memlock_too_low():
            self.skipTest('RLIMIT_MEMLOCK is too low')
        import json
        import os
        import shlex
        import shutil
        import stat
        import tempfile
        import time
        from unittest.mock import patch

        from kitty.constants import unserialize_launch_flag
        from kitty.fast_data_types import Secret
        from kitty.logout_session import (
            LOGOUT_CONFIRMATION_TIMEOUT,
            RESTORE_FAILED_FILE,
            RETRY_REQUESTED_FILE,
            ProcessState,
            claim_snapshots,
            delete_snapshot,
            finish_restore,
            is_logout_in_progress,
            load_snapshot,
            load_snapshot_for_restore,
            lock_snapshot_for_saver,
            new_key_id,
            prune_snapshots,
            read_key_id,
            read_snapshot,
            release_saver_lock,
            remove_all_snapshots,
            remove_orphaned_keys,
            remove_snapshot,
            restore_snapshots,
            retry_snapshot,
            save_snapshot,
        )
        from kitty.utils import log_error

        class FakeKeyStore:
            def __init__(self) -> None:
                self.keys: dict[str, Secret] = {}
                self.fail = False

            def store(self, key_id: str, key: Secret) -> None:
                if self.fail:
                    raise OSError('User interaction is not allowed')
                self.keys[key_id] = key

            def read(self, key_id: str) -> Secret | None:
                if self.fail:
                    raise OSError('User interaction is not allowed')
                return self.keys.get(key_id)

            def delete(self, key_id: str) -> None:
                self.keys.pop(key_id, None)

            def list_ids(self) -> list[str]:
                if self.fail:
                    raise OSError('User interaction is not allowed')
                return list(self.keys)

        errors: list[str] = []
        log_error.redirect = errors.append
        self.addCleanup(delattr, log_error, 'redirect')
        opts = self.set_options()
        tdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tdir, True)
        base = os.path.join(tdir, 'saved-at-logout')
        state = ProcessState(('--config', 'other.conf'), tdir, {'SECRET_VAR': 'value'}, 0)
        session = shlex.join(['launch', unserialize_launch_flag + json.dumps({'id': 7})])
        ks = FakeKeyStore()

        def save(pid: int, argv: tuple[str, ...] = state.argv) -> str:
            return save_snapshot(base, pid, session, {7: b'\x1b]52;c;eA==\x07seven', 8: b'eight'}, state._replace(argv=argv), ks)

        def mode(path: str) -> int:
            return stat.S_IMODE(os.stat(path).st_mode)

        def raw(path: str, name: str) -> bytes:
            with open(os.path.join(path, name), 'rb') as f:
                return f.read()

        # The saved state is readable only by the user and is encrypted with a key in the key store
        path = save(11)
        self.ae((mode(base), mode(path), mode(os.path.join(path, 'scrollback', '7')), mode(os.path.join(path, 'process.json'))), (0o700, 0o700, 0o600, 0o600))
        self.ae(list(ks.keys), [read_key_id(path)])
        for name, plaintext in (('process.json', b'SECRET_VAR'), ('session.kitty-session', b'launch'), ('scrollback/7', b'seven')):
            self.assertNotIn(plaintext, raw(path, name))
        # Repeated saves by the same process retain the three newest backups and their keys, ordered by saved_at, not mtime.
        paths = [path]
        for saved_at in (time.time() + 1, time.time() + 2, time.time() + 3):
            with patch('kitty.logout_session.time.time', return_value=saved_at):
                paths.append(save(11))
                key = ks.read(read_key_id(paths[-1]))
                self.ae(read_snapshot(paths[-1], key).saved_at, saved_at)
        self.assertFalse(os.path.exists(paths[0]))
        self.ae(set(os.listdir(base)), {os.path.basename(p) for p in paths[1:]})
        self.ae(set(ks.keys), {read_key_id(p) for p in paths[1:]})
        path = paths[-1]
        day_ago = time.time() - 86400
        os.utime(path, (day_ago, day_ago))
        for old in paths[1:-1]:
            remove_snapshot(old, ks)
        # Nothing is saved if the key cannot be stored, for example because the keychain needs user interaction
        ks.fail = True
        with self.assertRaises(OSError):
            save(12)
        ks.fail = False
        self.ae(os.listdir(base), [os.path.basename(path)])

        # Snapshots claimed by a running kitty instance are not claimed again, but those claimed by an instance that died are
        alive = {99}
        snapshot_name = os.path.basename(path)
        os.rename(path, os.path.join(base, f'{snapshot_name}.claimed-by-99'))
        self.ae(claim_snapshots(base, 1, ks, alive.__contains__), [])
        alive.clear()
        (snapshot,) = claim_snapshots(base, 1, ks, alive.__contains__)
        self.ae((snapshot.path, snapshot.process_state[:3]), (os.path.join(base, f'{snapshot_name}.claimed-by-1'), state[:3]))
        (s,) = load_snapshot(snapshot.path, opts, ks)
        # History and new output are visibly separated, even when history ended mid-line.
        restored = s.tabs[0].windows[0].restored_scrollback
        screen = self.create_screen(cols=100, lines=10, scrollback=20)
        screen.restore_scrollback(restored)
        from kitty.window import as_text

        lines = as_text(screen, add_history=True).splitlines()
        self.ae(lines[:3], ['seven', '', ''])
        self.assertTrue(lines[3].strip('-') == '' and len(lines[3]) >= 20)
        # Repeated crashes cap automatic attempts, but retain both the normal backup name and its encryption key.
        self.ae(len(claim_snapshots(base, 2, ks, alive.__contains__)), 1)
        self.ae(claim_snapshots(base, 3, ks, alive.__contains__), [])
        self.ae(os.listdir(base), [snapshot_name])
        self.assertIsNotNone(ks.read(read_key_id(path)))
        self.assertTrue(os.path.exists(os.path.join(path, RESTORE_FAILED_FILE)))
        self.assertTrue(errors)
        retry_snapshot(path)
        self.assertFalse(os.path.exists(os.path.join(path, 'attempts')))
        self.assertTrue(os.path.exists(os.path.join(path, RESTORE_FAILED_FILE)))
        (snapshot,) = claim_snapshots(base, 3, ks, alive.__contains__)
        load_snapshot(snapshot.path, opts, ks)
        finish_restore(snapshot.path)
        self.assertFalse(os.path.exists(os.path.join(path, RESTORE_FAILED_FILE)))
        remove_snapshot(path, ks)

        # Inaccessible keys and corrupt ciphertext are retained, with durable protection from later pruning.
        def modify(path: str) -> None:
            data = bytearray(raw(path, 'process.json'))
            data[-1] ^= 1
            with open(os.path.join(path, 'process.json'), 'wb') as f:
                f.write(data)

        def swap(path: str) -> None:
            os.replace(os.path.join(path, 'session.kitty-session'), os.path.join(path, 'process.json'))

        for name, damage, num_errors in (
            ('missing key', lambda path: ks.keys.clear(), 1),
            # both reading the key and listing keys to remove orphans fail
            ('keychain needs interaction', lambda path: setattr(ks, 'fail', True), 2),
            ('modified', modify, 1),
            ('swapped', swap, 1),
        ):
            with self.subTest(name):
                errors.clear()
                damaged_path = save(13)
                original_keys = dict(ks.keys)
                damage(damaged_path)
                self.ae(claim_snapshots(base, 1, ks, alive.__contains__), [])
                ks.fail = False
                self.ae((os.listdir(base), len(errors)), ([os.path.basename(damaged_path)], num_errors))
                self.assertTrue(os.path.exists(os.path.join(damaged_path, RESTORE_FAILED_FILE)))
                if name != 'missing key':
                    self.ae(ks.keys, original_keys)
                remove_snapshot(damaged_path, ks)

        # A transient key failure stays protected even after reads work again and newer saves trigger retention.
        protected = save(13)
        protected_key = read_key_id(protected)
        ks.fail = True
        prune_snapshots(base, ks)
        ks.fail = False
        for pid in range(50, 54):
            save_snapshot(base, pid, 'launch --cwd=/newer-empty', {}, state, ks)
        self.assertTrue(os.path.isdir(protected))
        self.assertIsNotNone(ks.read(protected_key))
        self.ae(len(os.listdir(base)), 4)  # Protected backup plus the newest three ordinary backups.
        retry_snapshot(protected)
        self.assertTrue(os.path.exists(os.path.join(protected, RETRY_REQUESTED_FILE)))
        with (
            patch('kitty.logout_session.snapshots_dir', return_value=base),
            patch('kitty.logout_session.get_key_store', return_value=ks),
        ):
            restored = restore_snapshots(opts)
        assert restored is not None
        self.ae(restored.path, protected + f'.claimed-by-{os.getpid()}')
        self.assertTrue(restored.sessions[0].tabs[0].windows[0].restored_scrollback.startswith(b'seven'))
        finish_restore(restored.path)
        self.assertFalse(os.path.exists(os.path.join(protected, RETRY_REQUESTED_FILE)))
        self.assertFalse(os.path.exists(os.path.join(protected, RESTORE_FAILED_FILE)))
        shutil.rmtree(base)
        ks.keys.clear()

        # Failed parsing releases the claim, but preserves attempts and protection; disabling only deletes ordinary backups.
        protected = save(13)
        key = ks.read(read_key_id(protected))
        assert key is not None
        from kitty.logout_session import encrypt

        with open(os.path.join(protected, 'session.kitty-session'), 'wb') as f:
            f.write(encrypt(key, 'session.kitty-session', b'invalid-session-command'))
        (snapshot,) = claim_snapshots(base, 1, ks, alive.__contains__)
        with patch('kitty.logout_session.get_key_store', return_value=ks):
            self.assertIsNone(load_snapshot_for_restore(snapshot.path, opts))
        self.assertTrue(os.path.isdir(protected))
        self.assertFalse(os.path.exists(snapshot.path))
        self.ae(raw(protected, 'attempts'), b'1')
        self.assertTrue(os.path.exists(os.path.join(protected, RESTORE_FAILED_FILE)))
        ordinary = save(14)
        unreadable = save(15)
        modify(unreadable)
        with (
            patch('kitty.logout_session.snapshots_dir', return_value=base),
            patch('kitty.logout_session.get_key_store', return_value=ks),
        ):
            remove_all_snapshots()
            self.assertTrue(os.path.isdir(base))
            self.assertTrue(os.path.isdir(protected))
            self.assertTrue(os.path.isdir(unreadable))
            self.assertFalse(os.path.exists(ordinary))
            self.ae(set(ks.keys), {read_key_id(protected), read_key_id(unreadable)})
            delete_snapshot(protected)
            delete_snapshot(unreadable)
        shutil.rmtree(base)
        self.ae(ks.keys, {})

        # Invalid attempt bookkeeping is recoverable rather than a reason to delete the backup.
        path = save(13)
        with open(os.path.join(path, 'attempts'), 'w') as f:
            f.write('invalid')
        self.ae(claim_snapshots(base, 1, ks, alive.__contains__), [])
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(os.path.exists(os.path.join(path, RESTORE_FAILED_FILE)))
        retry_snapshot(path)
        (snapshot,) = claim_snapshots(base, os.getpid(), ks, alive.__contains__)
        with (
            patch('kitty.logout_session.MAX_SNAPSHOTS', 0),
            patch('kitty.logout_session.snapshots_dir', return_value=base),
            patch('kitty.logout_session.get_key_store', return_value=ks),
        ):
            prune_snapshots(base, ks)
            remove_all_snapshots()
        self.assertTrue(os.path.isdir(snapshot.path))
        load_snapshot(snapshot.path, opts, ks)
        finish_restore(snapshot.path)
        self.assertFalse(os.path.exists(os.path.join(path, 'attempts')))
        self.assertFalse(os.path.exists(os.path.join(path, RESTORE_FAILED_FILE)))
        remove_snapshot(path, ks)

        # Even before a restore is attempted, corrupted session or scrollback files are not pruned.
        for name in ('session.kitty-session', 'scrollback/8'):
            path = save(13)
            data = bytearray(raw(path, name))
            data[-1] ^= 1
            with open(os.path.join(path, name), 'wb') as f:
                f.write(data)
            with patch('kitty.logout_session.MAX_SNAPSHOTS', 0):
                prune_snapshots(base, ks)
            self.assertTrue(os.path.isdir(path))
            self.assertTrue(os.path.exists(os.path.join(path, RESTORE_FAILED_FILE)))
            self.assertIsNotNone(ks.read(read_key_id(path)))
            remove_snapshot(path, ks)

        # An unreadable key id prevents unsafe orphan cleanup of that backup's key.
        path = save(13)
        key_id = read_key_id(path)
        os.unlink(os.path.join(path, 'key-id'))
        os.mkdir(os.path.join(path, 'key-id'))
        remove_orphaned_keys(base, ks)
        self.assertIsNotNone(ks.read(key_id))
        self.assertTrue(os.path.exists(os.path.join(path, RESTORE_FAILED_FILE)))
        shutil.rmtree(base)
        ks.keys.clear()

        # Keys of snapshots deleted by something other than kitty are deleted, but not the keys of existing snapshots or keys
        # not created by kitty
        path = save(15)
        orphan, foreign = new_key_id(), 'some-other-item'
        ks.keys[orphan] = ks.keys[foreign] = ks.keys[read_key_id(path)]
        remove_orphaned_keys(base, ks)
        self.ae(sorted(ks.keys), sorted((read_key_id(path), foreign)))
        # Orphans are removed when saving and claiming, and all keys when all snapshots are removed
        ks.keys[orphan] = ks.keys[foreign]
        remove_orphaned_keys(base, ks)
        self.assertNotIn(orphan, ks.keys)
        ks.keys[orphan] = ks.keys[foreign]
        self.ae(len(claim_snapshots(base, 1, ks, alive.__contains__)), 1)
        self.assertNotIn(orphan, ks.keys)
        # Failing to list the keys is logged, not raised
        errors.clear()
        ks.fail = True
        remove_orphaned_keys(base, ks)
        ks.fail = False
        self.ae(len(errors), 1)
        shutil.rmtree(base)
        ks.keys.clear()

        # Files are bound to their names, so swapping the scrollback of two windows is detected
        path = save(14)
        os.rename(os.path.join(path, 'scrollback', '7'), os.path.join(path, 'scrollback', 'x'))
        os.rename(os.path.join(path, 'scrollback', '8'), os.path.join(path, 'scrollback', '7'))
        with self.assertRaises(Exception):
            load_snapshot(path, opts, ks)
        shutil.rmtree(base)
        ks.keys.clear()

        # The snapshot of a kitty instance that is still running, for example, because the logout was cancelled, is not claimed,
        # even if a process with its pid is running, since pids are reused
        import kitty.logout_session as ls

        path = save(16)
        lock_snapshot_for_saver(path)
        alive.add(16)
        self.ae(claim_snapshots(base, 1, ks, alive.__contains__), [])
        release_saver_lock(os.path.join(base, 'other'))
        self.ae(claim_snapshots(base, 1, ks, alive.__contains__), [])
        with patch('kitty.logout_session.MAX_SNAPSHOTS', 0):
            prune_snapshots(base, ks)
        self.assertTrue(os.path.isdir(path))
        release_saver_lock(path)
        self.ae(len(claim_snapshots(base, 1, ks, alive.__contains__)), 1)
        alive.clear()
        # Deleting the snapshot of this instance, as when the logout is cancelled, releases its lock
        path = save(17)
        lock_snapshot_for_saver(path)
        delete_snapshot(path)
        self.assertIsNone(ls.saver_lock)
        shutil.rmtree(base)
        ks.keys.clear()

        # Without a key store, as on Linux, snapshots are not encrypted and are readable only by the user
        path = save_snapshot(base, 31, session, {7: b'seven'}, state, None)
        self.ae((read_key_id(path), mode(path), mode(os.path.join(path, 'process.json'))), ('', 0o700, 0o600))
        self.assertIn(b'SECRET_VAR', raw(path, 'process.json'))
        (snapshot,) = claim_snapshots(base, 1, None, alive.__contains__)
        self.ae(snapshot.process_state[:3], state[:3])
        (s,) = load_snapshot(snapshot.path, opts, None)
        self.assertTrue(s.tabs[0].windows[0].restored_scrollback.startswith(b'seven\r\n'))
        finish_restore(snapshot.path)
        self.assertTrue(os.path.isdir(path))
        (snapshot,) = claim_snapshots(base, 1, None, alive.__contains__)
        finish_restore(snapshot.path)
        self.assertTrue(os.path.isdir(path))
        shutil.rmtree(base)
        # An encrypted snapshot cannot be read without a key store and an unencrypted one is not accepted when there is a key store
        errors.clear()
        plain = save_snapshot(base, 32, session, {}, state, None)
        self.ae(claim_snapshots(base, 1, ks, alive.__contains__), [])
        encrypted = save(33)
        (snapshot,) = claim_snapshots(base, 1, None, alive.__contains__)
        self.ae(snapshot.path, plain + '.claimed-by-1')
        finish_restore(snapshot.path)
        self.ae(set(os.listdir(base)), {os.path.basename(plain), os.path.basename(encrypted)})
        self.ae(len(errors), 2)
        remove_orphaned_keys(base, ks)
        self.ae(set(ks.keys), {read_key_id(encrypted)})
        shutil.rmtree(base)
        ks.keys.clear()

        # Only the latest backup is restored, regardless of argv. Successful restoration keeps its data and encryption key.
        older = save(21, ('b',))
        latest = save(22, ('a',))
        os.utime(latest, (day_ago, day_ago))
        with (
            patch('kitty.logout_session.snapshots_dir', return_value=base),
            patch('kitty.logout_session.get_key_store', return_value=ks),
        ):
            for _ in range(4):
                r = restore_snapshots(opts)
                assert r is not None
                self.ae(os.path.basename(r.path), f'{os.path.basename(latest)}.claimed-by-{os.getpid()}')
                self.ae(len(r.sessions), 1)
                finish_restore(r.path)
                self.assertTrue(os.path.isdir(latest))
                self.assertTrue(os.path.isdir(older))
                self.ae(set(ks.keys), {read_key_id(latest), read_key_id(older)})
            remove_all_snapshots()
            self.assertFalse(os.path.exists(base))
            self.ae(ks.keys, {})
            self.assertIsNone(restore_snapshots(opts))

        # On Linux, the snapshot is saved when the desktop portal says the session is about to end and is deleted if the logout is
        # cancelled
        from types import SimpleNamespace

        from kitty.boss import Boss
        from kitty.logout_session import DESKTOP_SESSION_ENDING, DESKTOP_SESSION_QUERY_END, DESKTOP_SESSION_RUNNING

        self.set_options()  # on_desktop_session_state_changed consults persist_session
        boss = SimpleNamespace(snapshot_saved_for_logout='', save_session_snapshot=lambda: save_snapshot(base, 41, session, {}, state, None))
        for new_state, expected in (
            (DESKTOP_SESSION_QUERY_END, True),
            (DESKTOP_SESSION_RUNNING, False),
            (DESKTOP_SESSION_QUERY_END, True),
            (DESKTOP_SESSION_ENDING, True),
        ):
            Boss.on_desktop_session_state_changed(boss, new_state)  # type: ignore[arg-type]
            self.ae(bool(os.listdir(base)), expected)
        shutil.rmtree(base)

        # A quit confirmed long after the OS asked kitty to quit for a logout is an ordinary quit
        for requested_at, now, expected in ((-1, 10, False), (5, 10, True), (5, 6 + LOGOUT_CONFIRMATION_TIMEOUT, False)):
            with (
                patch('kitty.logout_session.application_logout_requested_at', return_value=requested_at),
                patch('kitty.logout_session.monotonic', return_value=now),
            ):
                self.ae(is_logout_in_progress(), expected)

    def test_agent_resume_in_session(self) -> None:
        encoded = unserialize_launch_flag + '{"id": 7, "agent": "claude"}'
        session_var = '--var=KITTY_AGENT_SESSION=019a90db-05e8-7c50'

        def window(line: str, **overrides: object) -> object:
            opts = self.set_options({'resume_agents_on_restore': True, **overrides})
            (session,) = parse_session(line, opts)
            return session.tabs[0].windows[0]

        # The command is built from the manifest and the reported session id, and the window is held open so that the output of an
        # agent that exits stays readable. It runs through the configured shell so shell startup files are loaded first.
        w = window(f'launch {shlex.join((encoded,))} {session_var}')
        self.ae(w.launch_spec.args[-2:], ['-ilc', 'exec claude --resume 019a90db-05e8-7c50'])
        self.assertTrue(w.launch_spec.opts.hold)

        # Resuming is opt in: the window is restored as an ordinary one, keeping the vars so a later save does not lose them
        w = window(f'launch {shlex.join((encoded,))} {session_var}', resume_agents_on_restore=False)
        self.ae(w.launch_spec.args, [])
        self.assertFalse(w.launch_spec.opts.hold)
        self.assertIn('KITTY_AGENT_SESSION=019a90db-05e8-7c50', w.launch_spec.opts.var)

        # A session id is reported by the agent, so a value that could be an option, a path or a shell command is never used
        for bad in ('--dangerous-flag', '/etc/passwd', 'a;rm -rf /', '$(id)', 'a b', '', 'x' * 513):
            w = window(f'launch {shlex.join((encoded, f"--var=KITTY_AGENT_SESSION={bad}"))}')
            self.ae(w.launch_spec.args, [], f'session id {bad!r} was accepted')

        # Without an id there is nothing to resume, and an agent kitty has no manifest for cannot be resumed
        self.ae(window(f'launch {shlex.join((encoded,))}').launch_spec.args, [])
        no_resume = unserialize_launch_flag + '{"id": 7, "agent": "gemini"}'
        self.ae(window(f'launch {shlex.join((no_resume,))} {session_var}').launch_spec.args, [])
        # The agent recorded in the session has to be one kitty knows
        bogus = unserialize_launch_flag + '{"id": 7, "agent": "../../bin/sh"}'
        self.ae(window(f'launch {shlex.join((bogus,))} {session_var}').launch_spec.args, [])

        # Sessions saved before this feature have no agent field
        old = unserialize_launch_flag + '{"id": 7}'
        self.ae(window(f'launch {shlex.join((old,))} {session_var}').launch_spec.args, [])

    def test_agent_resume_path_only_in_session(self) -> None:
        encoded = unserialize_launch_flag + '{"id": 7, "agent": "omp"}'
        session_path = '/Users/test/.omp/sessions/abc.jsonl'
        opts = self.set_options({'resume_agents_on_restore': True})
        (session,) = parse_session(f'launch {shlex.join((encoded,))} --var=KITTY_AGENT_SESSION_PATH={session_path}', opts)
        window = session.tabs[0].windows[0]
        self.ae(window.launch_spec.args[-2:], ['-ilc', f'exec omp --resume={session_path}'])
        self.assertTrue(window.launch_spec.opts.hold)

    def test_agent_resume_loads_shell_environment(self) -> None:
        for shell_name in ('zsh', 'bash', 'fish'):
            shell = shutil.which(shell_name)
            if shell is None:
                continue
            with self.subTest(shell=shell_name), tempfile.TemporaryDirectory() as home:
                home = os.path.realpath(home)
                bindir = os.path.join(home, 'bin')
                os.mkdir(bindir)
                for agent in ('claude', 'omp'):
                    executable = os.path.join(bindir, agent)
                    with open(executable, 'w') as f:
                        f.write('#!/bin/sh\nprintf "RESUMED:%s\\n" "$AGENT_RESUME_ENV" "$AGENT_LOGIN_ENV" "$PWD" "$@"\n')
                    os.chmod(executable, 0o700)
                if shell_name == 'fish':
                    confdir = os.path.join(home, '.config', 'fish')
                    os.makedirs(confdir)
                    files = {
                        'config.fish': f'set -gx AGENT_RESUME_ENV loaded\nset -gx PATH {shlex.quote(bindir)} $PATH\n'
                        'if status is-login; set -gx AGENT_LOGIN_ENV login; end\n'
                    }
                else:
                    confdir = home
                    rc = f'export AGENT_RESUME_ENV=loaded\nexport PATH={shlex.quote(bindir)}:$PATH\n'
                    files = (
                        {'.zshrc': rc, '.zprofile': 'export AGENT_LOGIN_ENV=login\n'}
                        if shell_name == 'zsh'
                        else {'.bashrc': rc, '.bash_profile': 'export AGENT_LOGIN_ENV=login\n. "$HOME/.bashrc"\n'}
                    )
                for name, content in files.items():
                    with open(os.path.join(confdir, name), 'w') as f:
                        f.write(content)
                env = {
                    'HOME': home,
                    'ZDOTDIR': home,
                    'XDG_CONFIG_HOME': os.path.join(home, '.config'),
                    'PATH': '/usr/bin:/bin',
                    'TERM': 'xterm',
                    'BASH_SILENCE_DEPRECATION_WARNING': '1',
                }
                opts = self.set_options({'resume_agents_on_restore': True, 'shell': shell, 'shell_integration': 'disabled'})
                session_path = os.path.join(home, "a 'quoted' $dollar; (session).jsonl")
                for agent, reference, expected in (
                    ('claude', '--var=KITTY_AGENT_SESSION=019a90db-05e8-7c50', ['--resume', '019a90db-05e8-7c50']),
                    ('omp', f'--var=KITTY_AGENT_SESSION_PATH={session_path}', [f'--resume={session_path}']),
                ):
                    encoded = unserialize_launch_flag + json.dumps({'id': 7, 'agent': agent, 'cmd_at_shell_startup': 'exit 99'})
                    (session,) = parse_session(f'launch {shlex.join((encoded, reference, "--cwd=" + home))}', opts)
                    spec = session.tabs[0].windows[0].launch_spec
                    result = subprocess.run(spec.args, cwd=spec.opts.cwd, env=env, capture_output=True, text=True, start_new_session=True, timeout=10)
                    self.ae(result.returncode, 0, result.stderr)
                    actual = [line.removeprefix('RESUMED:') for line in result.stdout.splitlines() if line.startswith('RESUMED:')]
                    self.ae(actual, ['loaded', 'login', home, *expected])

    def test_moved_windows_and_tabs_keep_session(self) -> None:
        # Drives the real Boss move methods and Tab detach/attach/take over code over fake OS Windows
        from contextlib import nullcontext
        from itertools import count

        from kitty.boss import Boss
        from kitty.layout.interface import Tall
        from kitty.tabs import Tab
        from kitty.window import Window as RealWindow
        from kitty.window_list import WindowList, reset_group_id_counter

        from .layout import Window as LayoutWindow
        from .layout import create_layout

        ids = count(1)

        class FakeWindow(LayoutWindow):
            effective_session_name = RealWindow.effective_session_name

            def __init__(self, session: str) -> None:
                super().__init__(next(ids))
                self.created_in_session_name = session
                self.tabref: Callable[[], FakeTab | None] = lambda: None

            def change_tab(self, tab: 'FakeTab') -> None:
                self.tabref = lambda: tab

        class FakeTab:
            name = cwd = ''
            enabled_layouts, _last_used_layout, _current_layout_name = ['tall'], None, ''

            def __init__(self, tm: 'FakeTabManager', session: str, *windows: FakeWindow) -> None:
                self.id, self.os_window_id, self.created_in_session_name = next(ids), tm.os_window_id, session
                self.tab_manager_ref = lambda: tm
                self.current_layout = create_layout(Tall)
                self.windows = WindowList(cast(Tab, self))
                real = 'remove_window detach_window _add_window _take_ownership_of_window attach_window attach_window_at_edge attach_windows set_active_window'
                for name in real.split():
                    setattr(self, name, getattr(Tab, name).__get__(self))
                for w in windows:
                    w.change_tab(self)
                    self._add_window(w)
                tm.tabs.append(self)

            def __len__(self) -> int:
                return len(self.windows)

            def __iter__(self) -> Iterator[FakeWindow]:
                return iter(cast(Iterator[FakeWindow], self.windows))

            def _add_window(self, w: FakeWindow) -> None:  # replaced by the real method
                pass

            def take_over_from(self, other: 'FakeTab') -> None:
                Tab.take_over_from(cast(Tab, self), cast(Tab, other))

            def mark_tab_bar_dirty(self) -> None:
                pass

            relayout = post_window_removal_update = active_window_changed = make_active = destroy = mark_tab_bar_dirty

        class FakeTabManager:
            def __init__(self, os_window_id: int, session: str = '') -> None:
                self.os_window_id, self.created_in_session_name = os_window_id, session
                self.tabs: list[FakeTab] = []

            def __len__(self) -> int:
                return len(self.tabs)

            def new_tab(self, empty_tab: bool = False, location: str = 'last', title: str = '') -> FakeTab:
                return FakeTab(self, '')

            def tab_at_location(self, location: str) -> FakeTab | None:
                return next((t for t in self.tabs if t.name == location), None)

            def remove(self, tab: FakeTab) -> None:
                self.tabs.remove(tab)

            def park_os_window_docks(self, tab: FakeTab) -> None:
                pass

            def sync_os_window_docks(self) -> None:
                pass

        class FakeBoss:
            shutting_down = False
            suppress_focus_change_events = nullcontext

            def __init__(self) -> None:
                self.os_window_map: dict[int, FakeTabManager] = {}
                real = '_move_window_to _move_tab_to _insert_window_in_direction _new_os_window_for_session _cleanup_tab_after_window_removal'
                for name in real.split():
                    setattr(self, name, getattr(Boss, name).__get__(self))

            def add_os_window(self) -> int:
                os_window_id = len(self.os_window_map) + 1
                self.os_window_map[os_window_id] = FakeTabManager(os_window_id)
                return os_window_id

            @property
            def all_tabs(self) -> Iterator[FakeTab]:
                for tm in self.os_window_map.values():
                    yield from tm.tabs

            def mark_os_window_for_close(self, os_window_id: int) -> None:
                pass

        def world() -> tuple[FakeBoss, FakeWindow, FakeWindow, FakeTab, FakeTab]:
            # Tab a of session a has a window of no session and one of session b, next to it is tab other of session c,
            # tab b of session b is in another OS Window
            boss = FakeBoss()
            reset_group_id_counter()
            for os_window_id, session in ((1, 'a'), (2, 'b')):
                boss.os_window_map[os_window_id] = FakeTabManager(os_window_id, session)
            unnamed, from_b = FakeWindow(''), FakeWindow('b')
            tab_a = FakeTab(boss.os_window_map[1], 'a', unnamed, from_b, FakeWindow(''))
            FakeTab(boss.os_window_map[1], 'c', FakeWindow('')).name = 'other'
            tab_b = FakeTab(boss.os_window_map[2], 'b', FakeWindow('b'))
            return boss, unnamed, from_b, tab_a, tab_b

        def sessions(w: FakeWindow) -> tuple[str, str, str]:
            tab = w.tabref()
            assert tab is not None
            tm = tab.tab_manager_ref()
            return w.effective_session_name, tab.created_in_session_name, tm.created_in_session_name

        patches = (patch('kitty.tabs.detach_window'), patch('kitty.tabs.attach_window'), patch('kitty.boss.current_os_window', return_value=1))
        with patches[0], patches[1], patches[2]:
            # A window keeps its own session, which may be none, wherever it goes. New tabs and OS Windows made for it
            # belong to its effective session, a window of no session in an existing tab belongs to that tab's session.
            # (window, target_tab_id, target_os_window_id) -> (window session, its tab's session, its OS Window's session)
            for which, target_tab_id, target_os_window_id, expected in (
                ('unnamed', 'new', None, ('a', 'a', 'a')),
                ('from_b', 'new', None, ('b', 'b', 'a')),
                ('unnamed', 'left', None, ('a', 'a', 'a')),  # no tab there, so a new one
                ('unnamed', 'other', None, ('c', 'c', 'a')),  # the existing tab there
                ('from_b', 'other', None, ('b', 'c', 'a')),
                ('unnamed', None, 'new', ('a', 'a', 'a')),
                ('from_b', None, 'new', ('b', 'b', 'b')),
                ('unnamed', 'tab_b', None, ('b', 'b', 'b')),
            ):
                boss, unnamed, from_b, tab_a, tab_b = world()
                w = unnamed if which == 'unnamed' else from_b
                own_session = w.created_in_session_name
                tid = tab_b.id if target_tab_id == 'tab_b' else target_tab_id
                cast(Boss, boss)._move_window_to(cast(RealWindow, w), tid, target_os_window_id)
                self.ae(sessions(w), expected, f'{which} {target_tab_id} {target_os_window_id}')
                self.ae(w.created_in_session_name, own_session)

            # Dropped next to a window in a tab of another session, the same as moving into that tab
            boss, unnamed, from_b, tab_a, tab_b = world()
            cast(Boss, boss)._insert_window_in_direction(cast(RealWindow, unnamed), cast(RealWindow, next(iter(tab_b))), 'left')
            self.ae((sessions(unnamed), unnamed.created_in_session_name), (('b', 'b', 'b'), ''))

            # A tab moved to a new OS Window, or an existing one, keeps its session and that of its windows
            for target_os_window_id, expected in ((None, ('a', 'a', 'a')), (2, ('a', 'a', 'b'))):
                boss, unnamed, from_b, tab_a, tab_b = world()
                cast(Boss, boss)._move_tab_to(cast(Tab, tab_a), target_os_window_id)
                self.ae(sessions(unnamed), expected)
                self.ae(sessions(from_b)[0], 'b')
                self.assertNotIn(tab_a, boss.os_window_map[1].tabs)

            # A window of no session in a tab of no session stays in no session
            boss, unnamed, from_b, tab_a, tab_b = world()
            tab_a.created_in_session_name = boss.os_window_map[1].created_in_session_name = ''
            cast(Boss, boss)._move_window_to(cast(RealWindow, unnamed), 'new')
            self.ae(sessions(unnamed), ('', '', ''))
