#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from kitty.agent_manifest import detect
from kitty.agents import AgentState, AgentTracker
from kitty.rc.agent import Agent
from kitty.rc.agent_read import AgentRead, read_terminal
from kitty.rc.base import MatchError, PayloadGetter, RemoteControlErrorWithoutTraceback, command_for_name, parse_subcommand_cli

from .agents import FakeWindow
from .base import BaseTest


class TestAgentRC(BaseTest):
    def setUp(self):
        self.command = Agent()
        self.window = FakeWindow('ready', focused=True)
        self.window.as_text = lambda: 'visible text'
        self.boss = SimpleNamespace(
            active_window=self.window,
            all_windows=[self.window],
            agent_tracker=AgentTracker(),
        )
        self.boss.match_windows = lambda match, origin: [w for w in self.boss.all_windows if match == 'all' or match == f'id:{w.id}']

    def response(self, action, /, **extra):
        payload = {'action': action, 'self': True, 'extent': 'detection', **extra}
        return json.loads(self.command.response_from_kitty(self.boss, self.window, PayloadGetter(self.command, payload)))

    def test_list_and_get_have_stable_agent_record(self):
        listed = self.response('list')
        self.assertEqual(listed[0], self.response('get'))
        self.assertEqual(
            set(listed[0]),
            {
                'id',
                'window_id',
                'agent',
                'state',
                'title',
                'cwd',
                'session_id',
                'focused',
                'generation',
                'cursor',
            },
        )
        self.assertEqual(listed[0]['agent'], 'claude')

    def test_explain_separates_screen_and_tracker(self):
        self.window.lines = ('✳ Thinking… (3s · esc to interrupt)',)
        self.boss.agent_tracker.refresh_window(self.window)
        self.window.lines = ('ready',)
        explained = self.response('explain')
        self.assertEqual(explained['tracker_state'], 'working')
        self.assertEqual(explained['arbitrated_state'], 'idle')
        self.assertEqual(
            explained['screen'],
            {
                'source': 'agent_detection_snapshot',
                'state': 'idle',
                'manifest': 'claude',
                'rule': None,
                'region': None,
                'priority': None,
                'evidence': '',
                'skipped': False,
            },
        )
        self.window.lines = ('✳ Thinking… (3s · esc to interrupt)',)
        match = detect('claude', self.window.agent_detection_snapshot())
        screen = self.response('explain')['screen']
        self.assertEqual(
            (screen['rule'], screen['region'], screen['priority'], screen['evidence']), (match.rule.id, match.rule.region, match.rule.priority, match.evidence)
        )

    def test_explain_skipped_screen_holds_tracker_state(self):
        self.window.child.foreground_processes = [{'cmdline': ['codex']}]
        self.window.lines = ('ready',)
        self.boss.agent_tracker.refresh_window(self.window)
        tracker = self.boss.agent_tracker
        tracker.snapshots[self.window.id] = replace(tracker.snapshot_for(self.window.id), state=AgentState.working)
        self.window.lines = ('› prompt', '↑/↓ to scroll pgup/pgdn to home/end to jump q to quit esc to edit prev')
        explained = self.response('explain')
        self.assertTrue(explained['screen']['skipped'])
        self.assertEqual(explained['screen']['rule'], 'transcript_viewer')
        self.assertEqual(explained['arbitrated_state'], 'working')

    def test_read_identifies_source(self):
        self.window.lines = ('live', '', 'tail')
        read = self.response('read', extent='detection')
        self.assertEqual(read['source'], 'agent_detection_snapshot')
        self.assertEqual(read['text'], 'live\n\ntail')
        visible = self.response('read', extent='visible')
        self.assertEqual(visible['source'], 'window.as_text')
        self.assertEqual(visible['text'], 'visible text')
        with self.assertRaises(RemoteControlErrorWithoutTraceback):
            self.response('read', extent='recent')

    def test_agent_read_sources_and_cli(self):
        calls = []
        self.window.as_text = lambda **kw: calls.append(kw) or 'old\nlong\rwrapped\ntail\n'
        self.assertEqual(read_terminal(self.window, 'recent', 2)['text'], 'wrapped\ntail')
        self.assertEqual(read_terminal(self.window, 'recent-unwrapped', 2)['text'], 'wrapped\ntail')
        self.assertEqual(calls, [{'add_history': True, 'add_wrap_markers': True}] * 2)
        self.assertEqual(read_terminal(self.window, 'detection', None)['text'], 'ready')
        command = AgentRead()
        self.assertIsInstance(command_for_name('agent-read'), AgentRead)
        opts, args = parse_subcommand_cli(command, ['agent-read', '--target', 'instance', '--source', 'recent'])
        payload = command.message_to_kitty(None, opts, args)
        self.assertEqual(payload['target'], 'instance')
        self.assertIsNone(payload['lines'])
        for value in (0, -1, 10001, True, '2'):
            with self.assertRaises(ValueError):
                read_terminal(self.window, 'recent', value)
        with self.assertRaises(ValueError):
            read_terminal(self.window, 'visible', 2)

    def test_agent_read_scrolled_viewport_and_blank_lines(self):
        calls = []

        def as_text(**kw):
            calls.append(kw)
            # A rolled viewport differs from the live bottom. History mode
            # ignores that viewport, as the real window.py helper does.
            return 'history\n\nlive\n\ntail\n' if kw.get('add_history') else 'rolled\n\nviewport'

        self.window.as_text = as_text
        self.window.lines = ('live', '', 'tail')
        self.assertEqual(read_terminal(self.window, 'visible', None)['text'], 'rolled\n\nviewport')
        self.assertEqual(read_terminal(self.window, 'detection', None)['text'], 'live\n\ntail')
        for source in ('recent', 'recent-unwrapped'):
            result = read_terminal(self.window, source, 4)
            self.assertEqual(result['text'], '\nlive\n\ntail')
            self.assertFalse(result['completeness']['complete_transcript'])
        self.assertEqual(calls, [{}, {'add_history': True, 'add_wrap_markers': True}, {'add_history': True, 'add_wrap_markers': True}])

    def test_non_agent_is_null_and_invalid_action_fails(self):
        self.window.child.foreground_processes = [{'cmdline': ['zsh']}]
        self.assertEqual(self.response('list'), [])
        for action in ('get', 'explain', 'read'):
            self.assertIsNone(self.response(action))
        with self.assertRaises(RemoteControlErrorWithoutTraceback):
            self.response('nope')

    def test_uncached_window_does_not_publish_to_tracker(self):
        self.assertNotIn(self.window.id, self.boss.agent_tracker.snapshots)
        self.response('get')
        self.assertNotIn(self.window.id, self.boss.agent_tracker.snapshots)

    def test_cached_snapshots_do_not_refresh_or_consume_dirty(self):
        tracker = self.boss.agent_tracker
        tracker.refresh_window(self.window)
        tracker.mark_dirty(self.window.id)
        tracker.pending_idle_since[self.window.id] = 10
        tracker.unseen_done.add(self.window.id)
        with patch.object(tracker, 'refresh_window', side_effect=AssertionError('read must not refresh')):
            self.response('list')
            self.response('get')
            self.response('explain')
            self.response('read')
        self.assertEqual(tracker.dirty_windows, {self.window.id})
        self.assertEqual(tracker.pending_idle_since, {self.window.id: 10})
        self.assertEqual(tracker.unseen_done, {self.window.id})

    def test_cached_none_does_not_hide_new_agent(self):
        tracker = self.boss.agent_tracker
        self.window.child.foreground_processes = [{'cmdline': ['zsh']}]
        tracker.refresh_window(self.window)
        self.assertIsNone(tracker.snapshot_for(self.window.id))
        self.window.child.foreground_processes = [{'cmdline': ['claude']}]
        self.window.lines = ('✳ Thinking… (3s · esc to interrupt)',)
        tracker.mark_dirty(self.window.id)
        for _ in range(2):
            self.assertEqual(self.response('get')['agent'], 'claude')
            self.assertEqual(self.response('list')[0]['state'], 'working')
            self.assertEqual(self.response('read')['record']['agent'], 'claude')
            explained = self.response('explain')
            self.assertEqual(explained['state_source'], 'live_observation')
            self.assertIsNone(explained['tracker_state'])
            self.assertEqual(explained['arbitrated_state'], 'working')
        self.assertEqual(tracker.snapshots, {self.window.id: None})
        self.assertEqual(tracker.dirty_windows, {self.window.id})

    def test_cached_agent_exited_to_shell_is_not_live(self):
        tracker = self.boss.agent_tracker
        tracker.refresh_window(self.window)
        cached = tracker.snapshot_for(self.window.id)
        self.window.child.foreground_processes = [{'cmdline': ['zsh']}]
        with patch.object(self.window, 'agent_detection_snapshot', side_effect=AssertionError('shell needs no screen')):
            self.assertEqual(self.response('list'), [])
            for action in ('get', 'explain', 'read'):
                self.assertIsNone(self.response(action))
        self.assertIs(tracker.snapshot_for(self.window.id), cached)

    def test_cached_agent_replaced_by_different_agent_uses_live_state(self):
        tracker = self.boss.agent_tracker
        self.window.lines = ('✳ Thinking… (3s · esc to interrupt)',)
        tracker.refresh_window(self.window)
        cached = tracker.snapshot_for(self.window.id)
        self.window.child.foreground_processes = [{'cmdline': ['codex']}]
        # A skipped overlay must not inherit the old Claude working state.
        self.window.lines = ('› prompt', '↑/↓ to scroll pgup/pgdn to home/end to jump q to quit esc to edit prev')
        self.assertEqual(self.response('get')['agent'], 'codex')
        self.assertEqual(self.response('get')['state'], 'unknown')
        self.assertEqual(self.response('list')[0]['agent'], 'codex')
        self.assertEqual(self.response('read')['record']['agent'], 'codex')
        explained = self.response('explain')
        self.assertEqual(explained['state_source'], 'live_observation')
        self.assertIsNone(explained['tracker_state'])
        self.assertEqual(explained['screen']['manifest'], 'codex')
        self.assertEqual(explained['arbitrated_state'], 'unknown')
        self.assertIs(tracker.snapshot_for(self.window.id), cached)

    def test_session_change_does_not_reuse_cached_state(self):
        tracker = self.boss.agent_tracker
        self.window.lines = ('✳ Thinking… (3s · esc to interrupt)',)
        tracker.refresh_window(self.window)
        cached = tracker.snapshot_for(self.window.id)
        self.window.lines = ('ready',)
        self.window.title = 'Renamed'
        self.window.cwd_of_child = '/new/path'
        self.window.user_vars = {'KITTY_AGENT_SESSION': 'new-session'}
        self.window.is_focused = False
        for record in (self.response('get'), self.response('list')[0], self.response('read')['record']):
            self.assertEqual(record['state'], 'idle')
            self.assertEqual(record['title'], 'Renamed')
            self.assertEqual(record['cwd'], '/new/path')
            self.assertEqual(record['session_id'], 'new-session')
            self.assertFalse(record['focused'])
            self.assertEqual(record['id'], record['window_id'])
        explained = self.response('explain')
        self.assertEqual(explained['state_source'], 'live_observation')
        self.assertIsNone(explained['tracker_state'])
        self.assertEqual(explained['arbitrated_state'], 'idle')
        self.assertIs(tracker.snapshot_for(self.window.id), cached)

    def test_window_selectors_ordering_and_cardinality(self):
        other = FakeWindow('ready')
        other.id = 3
        shell = FakeWindow('shell')
        shell.id = 9
        shell.child.foreground_processes = [{'cmdline': ['zsh']}]
        self.boss.all_windows = [self.window, shell, other]
        self.assertEqual([r['id'] for r in self.response('list', self=False)], [3, 7])
        self.assertEqual(self.response('get', match='id:3')['id'], 3)
        self.assertEqual(self.response('list', match='id:3')[0]['id'], 3)
        with self.assertRaises(MatchError):
            self.response('get', match='id:999')
        with self.assertRaises(RemoteControlErrorWithoutTraceback):
            self.response('get', match='all')
        self.boss.active_window = other
        self.assertEqual(self.response('get', self=False)['id'], 3)
        self.boss.active_window = None
        with self.assertRaises(RemoteControlErrorWithoutTraceback):
            self.command.response_from_kitty(self.boss, None, PayloadGetter(self.command, {'action': 'get'}))

    def test_declarative_cli_and_go_argument_handling(self):
        self.assertIsInstance(command_for_name('agent'), Agent)
        opts, args = parse_subcommand_cli(self.command, ['agent', '--match', 'id:7', '--extent', 'visible', 'read'])
        self.assertEqual(self.command.message_to_kitty(None, opts, args), {'action': 'read', 'match': 'id:7', 'self': False, 'extent': 'visible'})
        code = '\n'.join(self.command.args.as_go_code('agent', {'action': 'choices.list.get.explain.read'}, set()))
        self.assertIn('case "list", "get", "explain", "read"', code)
        with patch.object(self.command, 'fatal', side_effect=ValueError):
            with self.assertRaises(ValueError):
                self.command.message_to_kitty(None, opts, ['prompt'])
