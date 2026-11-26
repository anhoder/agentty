#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

import os
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kitty.agents import detect_agent, project_key_for_cwd
from kitty.editors import detect_editor, editor_row

from .base import BaseTest


class TestEditors(BaseTest):
    def test_editor_detection(self):
        for name in ('nvim', 'vim', 'vi', 'view'):
            processes = [{'cmdline': [f'/usr/bin/{name}', 'file.txt']}]
            self.ae(detect_editor(processes), name)
            self.assertIsNone(detect_agent(processes))
        for argv in ([], ['sh', '-c', 'vim'], ['node', 'vim'], ['gvim'], ['nvim', '--headless'],
                     ['nvim', '--embed'], ['vim', '-es'], ['vim', '-g'], ['nvim', '--remote-send', 'x'],
                     ['vim', '--remote-tab'], ['nvim', '--version']):
            self.assertIsNone(detect_editor([{'cmdline': argv}]), argv)
        self.ae(detect_editor([{'cmdline': ['nvim', '--server', '/tmp/nvim', '--remote-ui']}]), 'nvim')
        self.ae(detect_editor([{'cmdline': ['vim', '--', '--headless']}]), 'vim')

    def test_additional_editor_detection(self):
        cases = (
            (['nano', 'file'], 'nano'),
            (['nano', '-v', 'file'], 'nano'),
            (['nano', '--version'], None),
            (['nano', '-h'], None),
            (['micro', 'file'], 'micro'),
            (['micro', '--version'], None),
            (['micro', '-help'], None),
            (['hx', 'file'], 'helix'),
            (['helix', 'file'], 'helix'),
            (['hx', '--tutor'], 'helix'),
            (['hx', '--version'], None),
            (['hx', '--health'], None),
            (['hx', '-g', 'fetch'], None),
            (['kak', 'file'], 'kakoune'),
            (['kak', '-ui', 'terminal', 'file'], 'kakoune'),
            (['kak', '-c', 'session', 'file'], 'kakoune'),
            (['kak', '-ui', 'dummy'], None),
            (['kak', '--ui=dummy'], None),
            (['kak', '-d', '-s', 'name'], None),
            (['kak', '-p', 'session'], None),
            (['kak', '-f', 'keys', 'file'], None),
            (['kak', '-l'], None),
            (['emacs', '-nw', 'file'], 'emacs'),
            (['emacs', '--no-window-system', 'file'], 'emacs'),
            (['emacs', 'file'], None),
            (['emacs', '--batch', '-nw', 'file'], None),
            (['emacs', '--daemon', '-nw'], None),
            (['emacsclient', '-t', 'file'], 'emacs'),
            (['emacsclient', '-nw', 'file'], 'emacs'),
            (['emacsclient', '-c', 'file'], None),
            (['emacsclient', '-e', '(x)'], None),
        )
        for argv, expected in cases:
            with self.subTest(argv=argv):
                self.ae(detect_editor([{'cmdline': argv}]), expected)
                self.assertIsNone(detect_agent([{'cmdline': argv}]))

    def test_editor_option_arguments(self):
        for option in ('-c', '--cmd', '-u', '-U', '-S', '-i', '-t'):
            for value in ('--headless', '--version', '--remote-send', 'script'):
                self.ae(detect_editor([{'cmdline': ['nvim', option, value, 'file']}]), 'nvim')
        self.ae(detect_editor([{'cmdline': ['vim', '-S', 'script']}]), 'vim')
        self.ae(detect_editor([{'cmdline': ['nvim', '--', '--headless']}]), 'nvim')
        self.assertIsNone(detect_editor([{'cmdline': ['nvim', '--headless', 'file']}]))
        self.assertIsNone(detect_editor([{'cmdline': ['nvim', '-c', 'echo 1', '--headless', 'file']}]))
        self.assertIsNone(detect_editor([{'cmdline': ['vim', '-s', 'script']}]))

    def test_editor_refresh_and_exit(self):
        from kitty.boss import Boss

        window = SimpleNamespace(id=12, title='file.py', cwd_of_child='/tmp/project', child=SimpleNamespace(foreground_processes=[]))
        tab = type('Tab', (list,), {})([window])
        class Manager(list):
            pass
        tm = Manager([tab])
        tm.agent_sidebar_rows = ()
        tm.update_native_agents_sidebar = Mock()

        def set_rows(rows):
            tm.agent_sidebar_rows = tuple(rows)
            tm.update_native_agents_sidebar()

        tm.set_agent_sidebar_rows = set_rows
        tracker = SimpleNamespace(snapshot_for=lambda wid: None)
        boss = SimpleNamespace(active_window=window, agent_tracker=tracker)
        with patch('kitty.boss.get_options', return_value=SimpleNamespace(sidecar_grouping='agent')):
            Boss._refresh_agent_sidebar_scope(boss, (tm,))
            self.ae(tm.agent_sidebar_rows[-1].window_id, 0)
            window.child.foreground_processes = [{'cmdline': ['vi']}]
            Boss._refresh_agent_sidebar_scope(boss, (tm,))
            row = tm.agent_sidebar_rows[-1]
            self.ae((row.window_id, row.agent, row.kind, row.detail), (12, 'vi', 'editor', 'project'))
            self.ae(tm.update_native_agents_sidebar.call_count, 2)
            Boss._refresh_agent_sidebar_scope(boss, (tm,))
            Boss._refresh_agent_sidebar_scope(boss, (tm,))
            self.ae(tm.update_native_agents_sidebar.call_count, 2)
            window.child.foreground_processes = [{'cmdline': ['zsh']}]
            Boss._refresh_agent_sidebar_scope(boss, (tm,))
            self.ae(tm.agent_sidebar_rows[-1].window_id, 0)
            self.ae(tm.update_native_agents_sidebar.call_count, 3)

    def test_editor_focus_and_unread_isolation(self):
        from kitty.agents import AgentSidebarRow, AgentState
        from kitty.boss import Boss

        window = SimpleNamespace(id=12)

        class Manager(list):
            pass

        tm = Manager([[window]])
        tm.os_window_id = 3
        tm.agent_sidebar_enabled = True
        tm.agent_sidebar_rows = (AgentSidebarRow(12, '', '', 'vi', '', '', AgentState.unknown, False, 'editor'),)
        boss = SimpleNamespace(
            os_window_map={3: tm}, window_id_map={12: window},
            agent_sidebar_scope=lambda source: (source,),
            set_active_window=Mock(return_value=3),
            agent_tracker=SimpleNamespace(mark_unseen_done=Mock(return_value=True)),
            flush_agent_sidebar_updates=Mock(),
        )
        boss.agents_sidebar_menu = lambda data: Boss.agents_sidebar_menu(boss, data)
        with patch('kitty.boss.set_agents_sidebar_focus'):
            Boss.agents_sidebar_click(boss, '3:0')
            boss.set_active_window.assert_called_once_with(window, switch_os_window_if_needed=True)
            Boss.agents_sidebar_menu(boss, '3:12:mark_unread')
            boss.agent_tracker.mark_unseen_done.assert_not_called()
            boss.flush_agent_sidebar_updates.assert_not_called()
            tm.agent_sidebar_rows = (AgentSidebarRow(12, '', '', 'claude', '', '', AgentState.idle, False),)
            Boss.agents_sidebar_menu(boss, '3:12:mark_unread')
            boss.agent_tracker.mark_unseen_done.assert_called_once_with(12)
            boss.flush_agent_sidebar_updates.assert_called_once_with()
            boss.set_active_window.reset_mock()
            boss.window_id_map[12] = SimpleNamespace(id=12)
            Boss.agents_sidebar_click(boss, '3:0')
            boss.set_active_window.assert_not_called()

    def test_editor_directory_detail(self):
        window = SimpleNamespace(id=1, title='title', child=SimpleNamespace(foreground_processes=[{'cmdline': ['vim']}]))
        for cwd, expected in (('/tmp/project', 'project'), ('/', '/'), ('', ''), (None, '')):
            with self.subTest(cwd=cwd):
                window.cwd_of_child = cwd
                self.ae(editor_row(window, True).detail, expected)
        window.cwd_of_child = None
        window.child.cwd = '/tmp/child-project'
        row = editor_row(window, True)
        self.ae(row.detail, 'child-project')
        self.ae(row.project_key, project_key_for_cwd('/tmp/child-project'))

    def test_project_key_groups_subdirectories(self):
        project_key_for_cwd.cache_clear()
        with tempfile.TemporaryDirectory() as tdir:
            root = os.path.join(tdir, 'repo')
            os.makedirs(os.path.join(root, '.git'))
            sub = os.path.join(root, 'src')
            os.makedirs(sub)
            other = os.path.join(tdir, 'other')
            os.makedirs(other)
            self.ae(project_key_for_cwd(sub), os.path.realpath(root))
            self.ae(project_key_for_cwd(root), os.path.realpath(root))
            self.ae(project_key_for_cwd(other), os.path.realpath(other))
            self.ae(project_key_for_cwd(''), '')
        project_key_for_cwd.cache_clear()

    def test_single_foreground_query(self):
        class Child:
            queries = 0

            @property
            def foreground_processes(self):
                self.queries += 1
                return [{'cmdline': ['nvim']}]

        child = Child()
        window = SimpleNamespace(id=1, title='title', cwd_of_child='/tmp', child=child)
        self.ae(editor_row(window, True).kind, 'editor')
        self.ae(child.queries, 1)

    def test_ten_field_bridge(self):
        from kitty.tabs import TabManager

        window = SimpleNamespace(id=1, title='title', cwd_of_child='/tmp', child=SimpleNamespace(foreground_processes=[{'cmdline': ['vim']}]))
        row = editor_row(window, True)
        tm = SimpleNamespace(os_window_id=3, agent_sidebar_enabled=True, agent_sidebar_cols=30, agent_sidebar_rows=(row,))
        with patch('kitty.tabs.is_macos', True), patch('kitty.tabs.get_boss', return_value=SimpleNamespace()), \
                patch('kitty.tabs.get_options', return_value=SimpleNamespace(sidecar_grouping='agent', sidecar_font_family='system')), \
                patch('kitty.tabs.set_agents_sidebar') as send:
            TabManager.update_native_agents_sidebar(tm)
        serialized = send.call_args.args[5][0]
        self.ae(len(serialized), 10)
        self.ae(serialized[8], 'editor')
        self.ae(serialized[9], row.project_key)
        self.assertTrue(row.project_key)
