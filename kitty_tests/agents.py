#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

from kitty.agent_manifest import (
    MISSING,
    Screen,
    StateAuthority,
    authority_for,
    detect,
    load_manifests,
    manifest_for,
    resolve_region,
    translate_regex,
)
from kitty.agents import AgentSidebarRow, AgentSnapshot, AgentState, AgentTracker, detect_agent, group_sidebar_rows, state_code, state_from_signals, state_icon

from .base import BaseTest


def screen(*lines: str, title: str = '', progress: str = '') -> Screen:
    return Screen(lines, title, progress)


class TestAgents(BaseTest):
    def test_sidebar_default_kind(self):
        row = AgentSidebarRow(1, '', '', 'claude', '', '', AgentState.idle, False)
        self.ae(row.kind, 'agent')

    def test_detect_agent_from_foreground_command(self):
        self.ae(detect_agent([{'cmdline': ['/usr/local/bin/claude', '--resume', 'x']}]), 'claude')
        self.ae(detect_agent([{'cmdline': ['node', '/opt/opencode/bin/opencode']}]), 'opencode')
        self.ae(detect_agent([{'cmdline': ['omp', '--agent', 'opencode']}]), 'omp')
        # Registry values are manifest ids, not display names
        self.ae(detect_agent([{'cmdline': ['antigravity']}]), 'agy')
        self.ae(detect_agent([{'cmdline': ['qoder']}]), 'qodercli')
        self.assertIsNone(detect_agent([{'cmdline': ['/bin/zsh', '-l']}]))
        self.assertIsNone(detect_agent([]))

    def test_detect_pi_from_resolved_node_entrypoint(self):
        for scope in ('@earendil-works', '@mariozechner'):
            path = f'/opt/homebrew/lib/node_modules/{scope}/pi-coding-agent/dist/cli.js'
            self.ae(detect_agent([{'cmdline': ['node', path]}]), 'pi')
        self.assertIsNone(detect_agent([{'cmdline': ['node', '/tmp/project/dist/cli.js']}]))

    # region slicing

    def test_bottom_non_empty_lines_skips_blank_padding(self):
        # An agent TUI pads the area below its status line with blank rows. A
        # raw physical-line tail sees only the padding.
        s = screen('user: go', '', '\u2733 Thinking\u2026 (12s \u00b7 esc to interrupt)', '', '', '', '')
        region = resolve_region('bottom_non_empty_lines(2)', s)
        self.ae(region.lines, ['user: go', '\u2733 Thinking\u2026 (12s \u00b7 esc to interrupt)'])

    def test_top_non_empty_lines(self):
        s = screen('', 'first', 'second', 'third')
        self.ae(resolve_region('top_non_empty_lines(2)', s).lines, ['first', 'second'])

    def test_bottom_lines_counts_physical_rows(self):
        # Unlike bottom_non_empty_lines, blank rows consume the budget
        s = screen('a', '', 'b', 'c')
        self.ae(resolve_region('bottom_lines(2)', s).lines, ['b', 'c'])
        self.ae(resolve_region('bottom_lines(3)', s).lines, ['', 'b', 'c'])

    def test_osc_regions(self):
        s = screen('body', title='\u280f | tmp', progress='4;1;-1')
        self.ae(resolve_region('osc_title', s).text, '\u280f | tmp')
        self.ae(resolve_region('osc_progress', s).text, '4;1;-1')
        # Absent OSC data is a missing region, not an empty one
        self.assertFalse(resolve_region('osc_progress', screen('body')).present)

    def test_after_last_horizontal_rule(self):
        s = screen('old', '\u2500\u2500\u2500\u2500', 'first block', '\u2500\u2500\u2500\u2500', 'live block')
        self.ae(resolve_region('after_last_horizontal_rule', s).lines, ['live block'])
        self.assertFalse(resolve_region('after_last_horizontal_rule', screen('no rule here')).present)

    def test_prompt_box_regions(self):
        s = screen(
            'transcript line',
            'status above box',
            '\u256d\u2500 header \u2500\u256e',
            '\u2502 \u276f typed text',
            '\u2570\u2500\u2500\u2500\u256f',
            'footer hint',
        )
        # Borders are stripped so a rule can anchor on the marker at column zero
        self.ae(resolve_region('prompt_box_body', s).lines, ['\u276f typed text'])
        self.ae(resolve_region('last_non_empty_above_prompt_box', s).text, 'status above box')
        self.assertFalse(resolve_region('prompt_box_body', screen('no box')).present)

    def test_prompt_marker_regions(self):
        s = screen('older output', '\u203a live prompt', 'queued below')
        self.ae(resolve_region('after_last_prompt_marker', s).lines, ['queued below'])
        self.ae(resolve_region('before_current_prompt_marker', s).lines, ['older output'])
        self.ae(
            resolve_region('whole_recent_without_current_prompt_marker', s).lines,
            ['older output', 'queued below'],
        )

    def test_unknown_region_is_missing(self):
        self.ae(resolve_region('no_such_region', screen('x')), MISSING)

    # gates

    def test_contains_is_conjunctive_and_regex_is_disjunctive(self):
        m = manifest_for('claude')
        rule = next(r for r in m.rules if r.id == 'live_blocked_form')
        # Footer alone is not enough; it co-occurs with working turns too.
        self.assertFalse(rule.gate.matches(resolve_region(rule.region, screen('esc to cancel'))))
        ok = resolve_region(rule.region, screen('\u2500\u2500\u2500\u2500', 'enter to confirm \u00b7 esc to cancel'))
        self.assertTrue(rule.gate.matches(ok))

    def test_contains_ignores_case(self):
        # The manifests depend on this. Claude's permission rule looks for
        # 'do you want to proceed?' while Claude Code draws it capitalised, so
        # a case-sensitive compare would never fire and a real approval prompt
        # would read as idle.
        prompt = screen(
            '\u2500' * 8,
            'Do you want to proceed?',
            '\u276f 1. Yes',
            '  2. No',
            'esc to cancel',
        )
        match = detect('claude', prompt)
        self.ae(match.state.value, 'blocked')
        self.ae(match.rule_id, 'generic_permission_prompt')

    def test_not_gate_blocks_match(self):
        m = manifest_for('codex')
        rule = next(r for r in m.rules if r.id == 'osc_title_idle')
        self.assertTrue(rule.gate.matches(resolve_region(rule.region, screen(title='tmp'))))
        # A spinner or an alert in the title is not idle
        self.assertFalse(rule.gate.matches(resolve_region(rule.region, screen(title='\u280b tmp'))))
        self.assertFalse(rule.gate.matches(resolve_region(rule.region, screen(title='Action Required'))))

    def test_rust_codepoint_escapes_are_translated(self):
        import re

        # Python has no \x{...} syntax, so the raw Rust pattern cannot compile
        self.assertRaises(re.error, re.compile, r'[\x{2800}-\x{28FF}]')
        # After translation it compiles and matches the intended code points
        braille = translate_regex(r'^[\x{2800}-\x{28FF}] ')
        self.assertTrue(re.search(braille, '\u2809 Thinking'))
        self.assertFalse(re.search(braille, 'x Idle'))
        # Codex's half-circle and braille title spinners
        self.assertTrue(re.search(translate_regex(r'[\x{25D0}-\x{25D3}]'), '\u25d1'))

    def test_rust_unicode_classes_and_u_escapes_are_translated(self):
        import re

        # \p{...} and \u{...} are Rust-only spellings. Plain \uXXXX is valid
        # Python already, so it is deliberately not in this list.
        for raw in (r'\p{Alphabetic}+', r'[\u{fe0e}\u{fe0f}]'):
            self.assertRaises(re.error, re.compile, raw)

        # Cursor's spinner rule needs all three at once
        spinner = re.compile(translate_regex(r'^\s*(\u2b21|\u2b22|[\u2800-\u28FF]+)\s+\p{Alphabetic}+\w*ing\b'))
        self.assertTrue(spinner.search('\u2b21 Thinking'))
        self.assertTrue(spinner.search('\u280f\u2819 Generating'))
        self.assertFalse(spinner.search('\u2b21 123ing'))

        # Hermes anchors on an emoji with an optional variation selector
        warn = re.compile(translate_regex(r'^\u26a0[\u{fe0e}\u{fe0f}]?(?:\s|$)'))
        self.assertTrue(warn.search('\u26a0 alert'))
        self.assertTrue(warn.search('\u26a0\ufe0f '))
        self.assertFalse(warn.search('x \u26a0'))

    def test_every_shipped_manifest_loads_completely(self):
        import os
        import tomllib

        from kitty.agent_manifest import MANIFEST_DIR, parse_manifest

        seen = 0
        for fn in sorted(os.listdir(MANIFEST_DIR)):
            if not fn.endswith('.toml'):
                continue
            with open(os.path.join(MANIFEST_DIR, fn), 'rb') as f:
                raw = tomllib.load(f)
            # A rule dropped for an unsupported region or an uncompilable
            # regex would silently weaken detection, so assert none are lost.
            m = parse_manifest(raw, fn)
            self.ae(len(m.rules), len(raw.get('rules', [])), f'{fn} lost rules')
            self.ae(m.id, fn[:-5])
            seen += 1
        self.assertGreater(seen, 20)

    # manifests

    def test_manifests_load_with_aliases(self):
        ms = load_manifests()
        for name in ('claude', 'codex', 'opencode'):
            self.assertIn(name, ms)
        self.ae(manifest_for('claude-code').id, 'claude')
        self.assertIsNone(manifest_for('nosuchagent'))

    def test_state_authority_follows_hook_coverage(self):
        # OpenCode's plugin reports every transition; Claude and Codex hooks
        # only report session identity.
        self.ae(authority_for('opencode'), StateAuthority.full_lifecycle)
        self.ae(authority_for('claude'), StateAuthority.identity_only)
        self.ae(authority_for('codex'), StateAuthority.identity_only)
        # omp has screen rules, so a report does not get to override them
        self.ae(authority_for('omp'), StateAuthority.identity_only)
        # An agent with no manifest has no screen rules at all, so a report is
        # the only evidence there is
        self.ae(authority_for('mastracode'), StateAuthority.full_lifecycle)

    def test_agent_without_rules_reports_unknown_not_idle(self):
        # mastracode is recognized by name but ships no rules. Saying idle
        # would present a guess as a fact.
        quiet = screen('some output with no markers')
        self.ae(state_from_signals('mastracode', [], quiet), AgentState.unknown)
        # a report is still honoured
        self.ae(state_from_signals('mastracode', [{'state': 'working'}], quiet), AgentState.working)

    def test_omp_title_and_prompt_header(self):
        # Titles verbatim from live omp panes. The marker after the pi glyph is
        # the signal; the pi glyph itself is present in both states, so a rule
        # anchored on the spinner alone never fires.
        working = screen(title='\u03c0 \u2819 \U0001f442 HERDR: Align kitty state monitoring with herdr')
        self.ae(detect('omp', working).state.value, 'working')
        self.ae(detect('omp', screen(title='\u03c0 \u283c \U0001f442 HERDR: Fix omp agent sidebar status')).state.value, 'working')
        idle = screen(title='\u03c0 > \U0001f310 I18N: Split i18n sync audit prompt')
        self.ae(detect('omp', idle).state.value, 'idle')
        # Title disabled: the prompt box header carries the same distinction,
        # and adds elapsed turn time.
        busy_box = screen('\u256d\u2500\u2500 \u28a7 2m > \u25d2 Opus 5 > \U0001f4c1 ~/Desktop/kitty', '\u2570\u2500')
        self.ae(detect('omp', busy_box).state.value, 'working')
        rest_box = screen('\u256d\u2500\u2500 \u03c0 > \u25d2 gpt-6.1-sol > \U0001f4c1 ~/x', '\u2570\u2500')
        self.ae(detect('omp', rest_box).state.value, 'idle')
        # The model glyph shows in both states and is not activity
        self.assertNotEqual(detect('omp', rest_box).state.value, 'working')

    def test_omp_question_waiting_is_blocked(self):
        self.ae(detect('omp', screen(title='π ! tmp')).state.value, 'blocked')
        form = ('│ Status probe │', '│ ❯ ◉ Yes      │', '│   Submit     │', '╰──────────────╯')
        for title in ('', 'π > tmp', 'π ⠙ tmp'):
            for heading in ('╭─ Ask ─────────╮', '╭─ Ask · 1 of 2 answered (30s) ─╮'):
                with self.subTest(title=title, heading=heading):
                    snapshot = screen(heading, *form, '╭── π > ⟳ model > /tmp', '╰─', title=title)
                    match = detect('omp', snapshot)
                    self.ae((match.state.value, match.rule_id), ('blocked', 'ask_form_blocked'))
                    self.ae(state_from_signals('omp', [], snapshot, AgentState.working), AgentState.blocked)
        # A tall form can scroll its Submit row out of view.
        self.ae(detect('omp', screen('╭─ Ask ─╮', '│ Question │', '╰────────╯')).state.value, 'blocked')
        hidden = screen('Ask hidden: Status probe · ⌥H/⏎ show · ⎋ composer', '╭── π > ⟳ model > /tmp', '╰─')
        self.ae(detect('oh-my-pi', hidden).state.value, 'blocked')
        self.ae(detect('omp', screen('Ask · 1 of 2 answered (30s) hidden: Probe · ⌥H/⏎ show · ⎋ composer')).state.value, 'blocked')

    def test_omp_question_markers_do_not_block_normal_output(self):
        for lines in (
            ('Please Ask the user, then Submit the answer.',),
            ('╭─ Ask · completed (54ms) ─╮', '│ User answers: yes │', '╰──────────────╯'),
            ('│   Submit     │',),
            ('Ask hidden: a quoted example without live controls',),
            ('π ! tmp', '╭── π > ⟳ model > /tmp', '╰─'),
        ):
            with self.subTest(lines=lines):
                self.ae(detect('omp', screen(*lines, title='π > tmp')).state.value, 'idle')

    # detection

    def test_codex_title_spinner_cycle(self):
        # Matches the trace from `herdr agent explain` on a live codex pane.
        m = detect('codex', screen(title='tmp'))
        self.ae((m.state.value, m.rule_id), ('idle', 'osc_title_idle'))
        m = detect('codex', screen(title='\u280f | tmp'))
        self.ae((m.state.value, m.rule_id), ('working', 'osc_title_working'))
        m = detect('codex', screen(title='Action Required'))
        self.ae((m.state.value, m.rule_id), ('blocked', 'osc_title_blocked'))

    def test_highest_priority_rule_wins(self):
        # Both the blocked title rule (1100) and the idle title rule (100)
        # could describe this title; the blocker must win.
        m = detect('codex', screen(title='Action Required'))
        self.ae(m.rule.priority, 1100)

    def test_skip_state_update_holds_prior_state(self):
        transcript = screen('showing detailed transcript', '\u2191\u2193 scroll', '? for shortcuts')
        m = detect('claude', transcript)
        self.assertTrue(m.skipped)
        self.ae(state_from_signals('claude', [], transcript, AgentState.working), AgentState.working)
        self.ae(state_from_signals('claude', [], transcript, AgentState.blocked), AgentState.blocked)

    def test_no_rule_match_falls_back_to_idle(self):
        # Screen churn is not evidence of work.
        quiet = screen('some neutral transcript text')
        for agent in ('claude', 'codex', 'opencode'):
            self.ae(state_from_signals(agent, [], quiet), AgentState.idle)

    def test_unknown_agent_is_unknown(self):
        self.ae(state_from_signals(None, [{'state': 'blocked'}]), AgentState.unknown)

    def test_lifecycle_reports_are_scoped_to_the_current_session(self):
        statuses = [
            {'id': 'other', 'state': 'working'},
            {'id': 'root', 'state': 'idle'},
        ]
        self.ae(state_from_signals('opencode', statuses, screen('neutral'), session_id='root'), AgentState.idle)
        statuses[1] = {'id': 'root', 'state': 'working'}
        self.ae(state_from_signals('opencode', statuses, screen('neutral'), session_id='root'), AgentState.working)
        self.ae(state_from_signals('opencode', statuses, screen('neutral'), session_id='missing'), AgentState.idle)

    def test_opencode_native_root_status_without_session_identity(self):
        quiet = screen('Orchestrator · gpt-6.1-sol')
        for state in ('working', 'blocked', 'idle', 'done', 'error'):
            with self.subTest(state=state):
                statuses = [
                    {'id': '', 'app': 'opencode', 'state': state},
                    {'id': 'ses_other', 'app': 'opencode', 'state': 'blocked'},
                ]
                expected = AgentState.done if state == 'error' else AgentState(state)
                self.ae(state_from_signals('opencode', statuses, quiet), expected)
        for statuses in (
            [{'id': 'ses_other', 'app': 'opencode', 'state': 'working'}],
            [{'id': '', 'app': 'other', 'state': 'working'}],
            [{'id': '', 'state': 'working'}],
        ):
            with self.subTest(statuses=statuses):
                self.ae(state_from_signals('opencode', statuses, quiet), AgentState.idle)
        statuses = [
            {'id': '', 'app': 'opencode', 'state': 'blocked'},
            {'id': 'root', 'app': 'opencode', 'state': 'working'},
        ]
        self.ae(state_from_signals('opencode', statuses, quiet, session_id='root'), AgentState.working)

    # arbitration

    def test_full_lifecycle_report_wins(self):
        working = screen(title='')
        statuses = [{'id': 'root', 'state': 'blocked'}]
        self.ae(state_from_signals('opencode', statuses, working, session_id='root'), AgentState.blocked)
        statuses[0] = {'id': 'root', 'state': 'done'}
        self.ae(state_from_signals('opencode', statuses, working, session_id='root'), AgentState.done)

    def test_root_idle_does_not_hide_busy_child_session(self):
        statuses = [
            {'id': 'root', 'state': 'idle'},
            {'id': 'root/child', 'state': 'working'},
            {'id': 'other', 'state': 'blocked'},
        ]
        self.ae(state_from_signals('opencode', statuses, screen('neutral'), session_id='root'), AgentState.working)
        from kitty.agents import status_scope

        self.ae(
            status_scope(statuses, 'root'),
            {
                'root_session': 'root',
                'matched_ids': ['root', 'root/child'],
                'ignored_ids': ['other'],
            },
        )

    def test_identity_only_report_is_discarded(self):
        # A cancelled Claude turn leaves a stale working report while the
        # screen shows the idle prompt box. The screen must win.
        idle = screen('\u256d\u2500\u2500\u256e', '\u2502 \u276f ', '\u2570\u2500\u2500\u256f')
        self.ae(state_from_signals('claude', [{'state': 'working'}], idle), AgentState.idle)
        # And a live spinner must not be overridden by a stale done record.
        busy = screen('\u2733 Thinking\u2026 (3s \u00b7 esc to interrupt)')
        self.ae(state_from_signals('claude', [{'state': 'done'}], busy), AgentState.working)

    def test_state_icons_are_stable(self):
        self.ae([state_icon(s) for s in AgentState], ['\xb7', '\u27f3', '\u25cf', '\u2713', '?'])

    def test_state_codes_are_stable(self):
        self.ae([state_code(s) for s in AgentState], [1, 2, 3, 4, 0])


    def test_mark_idle_agent_as_unseen_done(self):

        tracker = AgentTracker()
        snapshot = AgentSnapshot(7, 'omp', AgentState.idle, 'task', '/tmp')
        tracker.snapshots[7] = snapshot
        self.assertTrue(tracker.mark_unseen_done(7))
        self.ae(tracker.snapshot_for(7).state, AgentState.done)
        self.assertIn(7, tracker.unseen_done)
        self.assertFalse(tracker.mark_unseen_done(7))
        self.assertFalse(tracker.mark_unseen_done(99))

    def test_sidebar_grouping_strategies(self):
        rows = (
            AgentSidebarRow(1, 'Today', 'Today', 'omp', 'omp', 'tmp · idle', AgentState.idle, False),
            AgentSidebarRow(2, 'Today', 'Today', 'claude', 'claude', 'tmp · working', AgentState.working, False),
            AgentSidebarRow(3, 'Yesterday', 'Yesterday', 'omp', 'omp', 'tmp · done', AgentState.done, False),
        )
        by_time = group_sidebar_rows(rows, 'time')
        self.ae([r.window_id for r in by_time], [1, 2, 3])
        by_agent = group_sidebar_rows(rows, 'agent')
        self.ae([r.agent for r in by_agent], ['claude', 'omp', 'omp'])
        self.ae([r.group for r in by_agent], ['claude', 'omp', 'omp'])

    def test_sidebar_grouping_tab_and_path(self):
        rows = (
            AgentSidebarRow(1, 'Project A', 'Project A', 'omp', 'omp', 'a', AgentState.idle, False),
            AgentSidebarRow(2, 'Project B', 'Project B', 'claude', 'claude', 'b', AgentState.working, False),
            AgentSidebarRow(3, '/tmp/project', '/tmp/project', 'omp', 'omp', 'c', AgentState.done, False),
        )
        by_path = group_sidebar_rows(rows, 'path')
        self.ae([r.group for r in by_path], ['/tmp/project', 'Project A', 'Project B'])

    def test_sidebar_title_shows_agent_and_terminal_title(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from kitty.agents import AgentSnapshot, detect_agent, project_key_for_cwd
        from kitty.boss import Boss

        # Matched executable is resolved to the manifest id.
        self.ae(detect_agent([{'cmdline': ['/usr/bin/cursor-agent']}]), 'cursor')
        self.ae(detect_agent([{'cmdline': ['node', '/opt/x/qoder']}]), 'qodercli')
        self.ae(detect_agent([{'cmdline': ['/bin/zsh']}]), None)

        class TabManager(list):
            agent_sidebar_rows = ()

            def set_agent_sidebar_rows(self, rows):
                self.agent_sidebar_rows = tuple(rows)

        class Tab(list):
            pass

        windows = [SimpleNamespace(id=i, child=SimpleNamespace(foreground_processes=[{'cmdline': [command]}]))
                   for i, command in ((1, 'opencode'), (2, 'omp'))]
        tabs = [Tab([w]) for w in windows]
        for i, tab in enumerate(tabs, start=1):
            tab.id = i
            tab.sidebar_id = f'tab-{i}'
            tab.effective_title = 'T'
        scope = (TabManager(tabs),)
        tracker = AgentTracker()
        tracker.snapshots = {
            1: AgentSnapshot(1, 'opencode', AgentState.idle, 'OC | 你好问候', '/tmp'),
            2: AgentSnapshot(2, 'omp', AgentState.idle, '', '/tmp'),  # no title yet
        }
        boss = SimpleNamespace(active_window=windows[0], agent_tracker=tracker)
        with patch('kitty.boss.get_options', return_value=SimpleNamespace(sidecar_grouping='tab')):
            Boss._refresh_agent_sidebar_scope(boss, scope)
        by_id = {r.window_id: r for r in scope[0].agent_sidebar_rows}
        self.ae(by_id[1].title, 'opencode · OC | 你好问候')
        self.ae(by_id[1].agent, 'opencode')  # icon lookup keys on the manifest id
        self.ae(by_id[1].project_key, project_key_for_cwd('/tmp'))
        self.ae(by_id[2].title, 'omp')  # no title → agent name only

    def test_sidebar_same_title_tabs(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from kitty.agents import AgentSnapshot
        from kitty.boss import Boss

        class Tab(list):
            effective_title = 'Project'

        class TabManager(list):
            agent_sidebar_rows = ()

            def set_agent_sidebar_rows(self, rows):
                self.agent_sidebar_rows = tuple(rows)

        windows = [SimpleNamespace(id=i, child=SimpleNamespace(foreground_processes=[{'cmdline': ['omp']}])) for i in range(1, 12)]
        tabs = [Tab([window]) for window in windows[:10]]
        for i, tab in enumerate(tabs, start=1):
            tab.id = i
            tab.sidebar_id = f'tab-{i}'
        tabs[0].append(windows[10])
        scope = (TabManager(tabs[:1]), TabManager(tabs[1:]))
        tracker = AgentTracker()
        tracker.snapshots = {w.id: AgentSnapshot(w.id, 'omp', AgentState.idle, 'omp', '/tmp') for w in windows}
        boss = SimpleNamespace(active_window=windows[1], agent_tracker=tracker)
        with patch('kitty.boss.get_options', return_value=SimpleNamespace(sidecar_grouping='tab')):
            Boss._refresh_agent_sidebar_scope(boss, scope)
            rows = tuple(r for r in scope[0].agent_sidebar_rows if r.kind == 'agent')
            self.ae([r.window_id for r in rows], [1, 11, *range(2, 11)])
            self.ae(len({r.group for r in rows}), 10)
            self.ae(rows[0].group, rows[1].group)
            self.assertNotEqual(rows[0].group, rows[2].group)
            self.ae(scope[1].agent_sidebar_rows, scope[0].agent_sidebar_rows)
            self.ae([(r.kind, r.title) for r in scope[0].agent_sidebar_rows if r.kind == 'editor'], [('editor', 'No editors')])
            self.ae([r.window_id for r in rows if r.is_active], [2])

            # Reordering and closing tabs must update numbering, not membership.
            scope[1].reverse()
            scope[0].clear()
            Boss._refresh_agent_sidebar_scope(boss, scope)
            rows = tuple(r for r in scope[1].agent_sidebar_rows if r.kind == 'agent')
            self.ae([r.window_id for r in rows], list(range(10, 1, -1)))
            self.ae(len({r.group for r in rows}), 9)
            # A working agent retitles its tab constantly. The label changes
            # with it, but the identity the UI keys collapsed state on must not.
            before = {r.window_id: r.group_key for r in rows}
            for tab in scope[1]:
                tab.effective_title = 'Renamed by agent'
            Boss._refresh_agent_sidebar_scope(boss, scope)
            rows = tuple(r for r in scope[1].agent_sidebar_rows if r.kind == 'agent')
            self.assertTrue(all('Renamed by agent' in r.group for r in rows))
            self.ae({r.window_id: r.group_key for r in rows}, before)

    def test_sidebar_restored_and_new_tabs_stay_separate(self):
        import weakref
        from types import SimpleNamespace
        from unittest.mock import patch

        from kitty.boss import Boss
        from kitty.session import parse_session
        from kitty.tabs import Tab

        opts = self.set_options({'sidecar_grouping': 'tab'})
        (session,) = parse_session('new_tab restored\ntab_id tab-2\nlaunch', opts)

        class TabManager(list):
            args = SimpleNamespace(directory='/tmp')
            os_window_id = 1
            os_window_docks = []
            agent_sidebar_rows = ()

            def set_agent_sidebar_rows(self, rows):
                self.agent_sidebar_rows = tuple(rows)

        tm = TabManager()
        with (
            patch('kitty.tabs._tabs_by_sidebar_id', weakref.WeakValueDictionary()),
            patch('kitty.tabs.add_tab', side_effect=(1, 2, 3, 4)),
            patch('kitty.tabs.Borders'),
            patch.object(Tab, '_set_current_layout'),
        ):
            restored = Tab(tm, session.tabs[0], no_initial_window=True)
            fresh = Tab(tm, no_initial_window=True)
            fresh.name = 'fresh'
            duplicate = Tab(tm, session.tabs[0], no_initial_window=True)
            legacy = Tab(tm, SimpleNamespace(name='legacy'), no_initial_window=True)
            tm.extend((restored, fresh, duplicate, legacy))
            self.ae(restored.sidebar_id, 'tab-2')

            windows = [SimpleNamespace(id=i, child=SimpleNamespace(foreground_processes=[])) for i in range(1, 6)]
            restored.windows.all_windows.extend(windows[:2])
            for tab, window in zip((fresh, duplicate, legacy), windows[2:]):
                tab.windows.all_windows.append(window)
            tracker = AgentTracker()
            tracker.snapshots = {w.id: AgentSnapshot(w.id, 'omp', AgentState.idle, 'omp', '/tmp') for w in windows}
            boss = SimpleNamespace(active_window=windows[0], agent_tracker=tracker)
            with patch('kitty.boss.get_options', return_value=opts):
                Boss._refresh_agent_sidebar_scope(boss, (tm,))
                rows = [r for r in tm.agent_sidebar_rows if r.kind == 'agent']
                self.ae([r.window_id for r in rows], [1, 2, 3, 4, 5])
                self.ae([r.group for r in rows], ['1: restored', '1: restored', '2: fresh', '3: restored', '4: legacy'])
                self.ae(rows[0].group_key, 'tab:tab-2')
                self.ae(rows[0].group_key, rows[1].group_key)
                self.ae(len({r.group_key for r in rows}), 4)

                before = {r.window_id: r.group_key for r in rows}
                tm.reverse()
                restored.name = 'renamed'
                Boss._refresh_agent_sidebar_scope(boss, (tm,))
                rows = [r for r in tm.agent_sidebar_rows if r.kind == 'agent']
                self.ae([r.window_id for r in rows], [5, 4, 3, 1, 2])
                self.ae({r.window_id: r.group_key for r in rows}, before)

    def test_empty_sidebar_has_no_group(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from kitty.boss import Boss

        class TabManager(list):
            agent_sidebar_rows = ()

            def set_agent_sidebar_rows(self, rows):
                self.agent_sidebar_rows = tuple(rows)

        window = SimpleNamespace(id=1, child=SimpleNamespace(foreground_processes=[{'cmdline': ['/bin/zsh']}]))
        scope = (TabManager([[window]]),)
        boss = SimpleNamespace(active_window=window, agent_tracker=AgentTracker())
        with patch('kitty.boss.get_options', return_value=SimpleNamespace(sidecar_grouping='time')):
            Boss._refresh_agent_sidebar_scope(boss, scope)
        rows = scope[0].agent_sidebar_rows
        self.ae(len(rows), 2)
        self.ae((rows[0].group, rows[0].group_key, rows[0].agent, rows[0].title), ('', '', '', 'No AI agents'))
        self.ae((rows[1].group, rows[1].group_key, rows[1].agent, rows[1].title, rows[1].kind), ('', '', '', 'No editors', 'editor'))


class TestAgentSidebarActions(BaseTest):
    def test_sidebar_content_does_not_resize_but_visibility_does(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        from kitty.boss import Boss
        from kitty.tabs import TabManager

        manager = SimpleNamespace(agent_sidebar_enabled=True, update_native_agents_sidebar=Mock(), resize=Mock())
        rows = (AgentSidebarRow(1, '', '', 'claude', 'Changed', '', AgentState.working, True),)
        TabManager.set_agent_sidebar_rows(manager, rows)
        manager.update_native_agents_sidebar.assert_called_once()
        manager.resize.assert_not_called()
        self.ae(manager.agent_sidebar_rows, rows)
        boss = SimpleNamespace(active_tab_manager=manager, agent_sidebar_enabled=False, all_tab_managers=(manager,),
                               refresh_agent_sidebars=Mock(), agent_sidebar_timer=0)
        with patch('kitty.boss.is_macos', True):
            Boss.toggle_sidecar(boss)
        manager.resize.assert_called_once()
        boss.refresh_agent_sidebars.assert_called_once()
    def test_focus_refreshes_sidebar_active_and_project_relation(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        from kitty.tabs import Tab

        first = SimpleNamespace(id=1)
        second = SimpleNamespace(id=2)
        tab = SimpleNamespace(
            active_window=first,
            os_window_id=7,
            id=8,
            current_layout=SimpleNamespace(only_active_window_visible=False, update_visibility=Mock()),
            windows=SimpleNamespace(groups=[]),
            tab_manager_ref=lambda: None,
            relayout_borders=Mock(),
            mark_tab_bar_dirty=Mock(),
        )
        boss = SimpleNamespace(agent_sidebar_enabled=True, refresh_agent_sidebars=Mock())
        with patch('kitty.tabs.get_boss', return_value=boss), patch('kitty.tabs.set_active_window') as native:
            Tab.active_window_changed(tab)
        native.assert_called_once_with(7, 8, 1)
        boss.refresh_agent_sidebars.assert_called_once_with()
        tab.active_window = second
        with patch('kitty.tabs.get_boss', return_value=boss), patch('kitty.tabs.set_active_window') as native:
            Tab.active_window_changed(tab)
        native.assert_called_once_with(7, 8, 2)
        self.assertEqual(boss.refresh_agent_sidebars.call_count, 2)
    def test_tab_switch_refreshes_editor_rows_for_shared_sidebar(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        from kitty.tabs import TabManager

        first = SimpleNamespace(id=1)
        second = SimpleNamespace(id=2)
        first_tab = SimpleNamespace(id=11, relayout=Mock(), active_window=SimpleNamespace(id=101))
        second_tab = SimpleNamespace(id=12, relayout=Mock(), active_window=SimpleNamespace(id=102))
        manager = SimpleNamespace(
            tabs=[first_tab, second_tab], active_tab_idx=0, _active_tab_idx=0, active_tab=first_tab,
            agent_sidebar_enabled=True, mark_tab_bar_dirty=Mock(), os_window_id=7,
        )
        manager._set_active_tab = lambda idx: setattr(manager, '_active_tab_idx', idx)
        boss = SimpleNamespace(agent_sidebar_enabled=True, refresh_agent_sidebars=Mock(), agent_sidebar_scope=lambda manager: (manager,))
        with patch('kitty.tabs.get_boss', return_value=boss), patch('kitty.tabs.set_active_tab'), patch('kitty.tabs.sync_agents_sidebar_editor_selection') as sync:
            TabManager.set_active_tab_idx(manager, 1)
        boss.refresh_agent_sidebars.assert_called_once_with()
        sync.assert_called_once_with(7, 101)
        self.assertIs(manager.active_tab, first_tab)

    def sidebar(self):
        from types import MethodType, SimpleNamespace

        from kitty.boss import Boss

        class Manager(list):
            agent_sidebar_enabled = True
            agent_sidebar_cols = 30
            agent_sidebar_rows = ()
            native_refreshes = 0

            def resize(self):
                pass

            def update_native_agents_sidebar(self):
                self.native_refreshes += 1

        class Tab(list):
            pass

        source, sibling, outside = (Manager() for _ in range(3))
        windows = []
        for os_window_id, manager in enumerate((source, sibling, outside), 1):
            manager.os_window_id = os_window_id
            tab = Tab()
            tab.tab_manager_ref = lambda manager=manager: manager
            window = SimpleNamespace(id=os_window_id * 10, tabref=lambda tab=tab: tab)
            tab.append(window)
            manager.append(tab)
            manager.agent_sidebar_rows = (AgentSidebarRow(window.id, 'Title', f'tab:{os_window_id}', 'omp', 'omp', '', AgentState.idle, False),)
            windows.append(window)
        boss = SimpleNamespace(
            os_window_map={tm.os_window_id: tm for tm in (source, sibling, outside)},
            window_id_map={w.id: w for w in windows}, active_window=windows[0], active_tab_manager=source,
            agent_sidebar_enabled=True, agent_sidebar_collapsed_groups=set(), agent_sidebar_cols=30,
            agent_sidebar_scope=lambda tm: (source, sibling) if tm is source or tm is sibling else (outside,),
            closed=[], confirmations=[],
        )

        def activate(window, switch_os_window_if_needed=False):
            boss.active_window = window
            return window.tabref().tab_manager_ref().os_window_id

        def confirm(msg, callback, **kwargs):
            overlay = SimpleNamespace(actions_on_focus_change=[])
            boss.confirmations.append((callback, overlay))
            boss.active_window = overlay
            return overlay

        boss.set_active_window = activate
        boss.mark_window_for_close = boss.closed.append
        boss.close_windows_with_confirmation_msg = lambda *args: ('Running an agent.', 1)
        boss.confirm = confirm
        for name in ('agents_sidebar_menu', 'agents_sidebar_click', 'agents_sidebar_resize', 'focus_sidecar'):
            setattr(boss, name, MethodType(getattr(Boss, name), boss))
        return boss, source, sibling, outside, windows

    def test_sidebar_shortcuts_respect_rebinding_without_dispatching_terminal_actions(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from kitty.boss import Boss
        from kitty.fast_data_types import GLFW_MOD_SUPER, KeyEvent, SingleKey
        from kitty.keys import Mappings
        from kitty.options.utils import KeyDefinition

        self.set_options()
        mappings = Mappings(global_shortcuts={})
        mappings.keyboard_modes[''].keymap = {
            SingleKey(GLFW_MOD_SUPER, False, ord('k')): [KeyDefinition(definition='focus_sidecar')],
            SingleKey(GLFW_MOD_SUPER, False, ord('b')): [KeyDefinition(definition='new_window')],
        }
        actions = []
        boss = SimpleNamespace(
            mappings=mappings, active_window=None,
            focus_sidecar=lambda: actions.append('focus'),
            toggle_sidecar=lambda: actions.append('toggle'),
        )
        with patch('kitty.keys.get_boss', return_value=boss):
            self.assertFalse(Boss.dispatch_agents_sidebar_shortcut(boss, KeyEvent(ord('b'), mods=GLFW_MOD_SUPER)))
            self.assertTrue(Boss.dispatch_agents_sidebar_shortcut(boss, KeyEvent(ord('k'), mods=GLFW_MOD_SUPER)))
            self.ae(actions, ['focus'])
            mappings.keyboard_modes[''].keymap[SingleKey(GLFW_MOD_SUPER, False, ord('k'))] = [KeyDefinition(definition='toggle_sidecar')]
            self.assertTrue(Boss.dispatch_agents_sidebar_shortcut(boss, KeyEvent(ord('k'), mods=GLFW_MOD_SUPER)))
            self.ae(actions, ['focus', 'toggle'])
            boss.dispatch_agents_sidebar_shortcut = lambda ev: Boss.dispatch_agents_sidebar_shortcut(boss, ev)
            seen = []
            boss.active_window = SimpleNamespace(program_status=True, program_status_seen_by_user=lambda: seen.append(True))
            with patch('kitty.boss.is_macos', True):
                self.assertTrue(Boss.dispatch_possible_special_key(boss, KeyEvent(ord('k'), mods=GLFW_MOD_SUPER)))
            self.ae(seen, [])

    def test_sidebar_click_focuses_agent_in_another_tab(self):
        from types import MethodType, SimpleNamespace
        from unittest.mock import Mock, patch

        from kitty.boss import Boss
        from kitty.tabs import TabManager

        class Tab(list):
            def set_active_window(self, window, for_keep_focus=None):
                self.active_window = window

        manager = object.__new__(TabManager)
        first, target = SimpleNamespace(id=1), SimpleNamespace(id=2)
        first_tab, target_tab = Tab([first]), Tab([target])
        first_tab.active_window, target_tab.active_window = first, target
        for tab in (first_tab, target_tab):
            tab.relayout = Mock()
        first.tabref = lambda: first_tab
        target.tabref = lambda: target_tab
        manager.tabs = [first_tab, target_tab]
        manager.os_window_id = 7
        manager._active_tab_idx = 0
        manager.agent_sidebar_enabled = True
        manager.agent_sidebar_rows = (AgentSidebarRow(2, '', '', 'omp', 'Target', '', AgentState.idle, False),)
        manager.mark_tab_bar_dirty = Mock()
        manager._set_active_tab = lambda idx: setattr(manager, '_active_tab_idx', idx)
        manager.set_active_tab = lambda tab, **kw: TabManager.set_active_tab_idx(manager, manager.tabs.index(tab))

        class Controller:
            @property
            def active_tab(self):
                return manager.active_tab

            @property
            def active_window(self):
                return manager.active_window

        boss = Controller()
        boss.os_window_map = {7: manager}
        boss.window_id_map = {1: first, 2: target}
        boss.agent_sidebar_enabled = True
        boss.agent_sidebar_scope = lambda tm: (manager,)
        boss.refresh_agent_sidebars = Mock()
        for name in ('agents_sidebar_click', 'agents_sidebar_menu', 'set_active_window'):
            setattr(boss, name, MethodType(getattr(Boss, name), boss))
        with patch('kitty.tabs.get_boss', return_value=boss), patch('kitty.boss.set_agents_sidebar_focus'), \
                patch('kitty.boss.current_focused_os_window_id', return_value=7):
            boss.agents_sidebar_click('7:0')
        self.assertIs(manager.active_tab, target_tab)
        self.assertIs(boss.active_window, target)

    def test_sidebar_menu_rejects_targets_outside_native_tab_scope(self):
        from unittest.mock import patch

        boss, source, sibling, outside, windows = self.sidebar()
        with patch('kitty.boss.set_agents_sidebar_focus'):
            for command in ('focus', 'new_window', 'copy_cwd', 'close'):
                boss.agents_sidebar_menu(f'1:30:{command}')
            self.assertIs(boss.active_window, windows[0])
            self.ae(boss.confirmations, [])
            boss.agents_sidebar_menu('1:20:focus')
            self.assertIs(boss.active_window, windows[1])

    def test_sidebar_stale_row_cannot_activate_moved_or_closed_window(self):
        from unittest.mock import patch

        boss, source, sibling, outside, windows = self.sidebar()
        source[0].remove(windows[0])
        outside[0].append(windows[0])
        boss.active_window = windows[1]
        with patch('kitty.boss.set_agents_sidebar_focus'):
            boss.agents_sidebar_click('1:0')
            self.assertIs(boss.active_window, windows[1])
            del boss.window_id_map[windows[0].id]
            boss.agents_sidebar_menu('1:10:close')
            self.ae(boss.confirmations, [])

    def test_sidebar_view_commands_preserve_other_scope_groups(self):
        boss, source, sibling, outside, windows = self.sidebar()
        boss.agent_sidebar_collapsed_groups = {'tab:3'}
        boss.agents_sidebar_menu('1:0:collapse_all')
        self.ae(boss.agent_sidebar_collapsed_groups, {'tab:1', 'tab:2', 'tab:3'})
        boss.agents_sidebar_menu('1:0:expand_all')
        self.ae(boss.agent_sidebar_collapsed_groups, {'tab:3'})
        self.ae((source.native_refreshes, sibling.native_refreshes, outside.native_refreshes), (2, 2, 0))

    def test_sidebar_width_commands_share_mouse_resize_limits(self):
        from unittest.mock import patch

        boss, source, sibling, outside, windows = self.sidebar()
        with patch('kitty.boss.viewport_for_window', return_value=(None, None, 400, 300, 10, 20)):
            boss.agents_sidebar_menu('1:0:width_increase')
            self.ae((source.agent_sidebar_cols, sibling.agent_sidebar_cols, outside.agent_sidebar_cols), (30, 30, 30))
            boss.agents_sidebar_menu('1:0:width_decrease')
            self.ae((source.agent_sidebar_cols, sibling.agent_sidebar_cols), (28, 28))
            boss.agents_sidebar_resize('1:999')
            self.ae(source.agent_sidebar_cols, 30)
            boss.agents_sidebar_resize('1:0')
            boss.agents_sidebar_menu('1:0:width_decrease')
            self.ae((source.agent_sidebar_cols, boss.agent_sidebar_cols), (16, 16))

    def test_sidebar_close_cancel_restores_focus_without_overriding_user_switch(self):
        from unittest.mock import patch

        for switched in ('none', 'window', 'os_window', 'closed', 'hidden'):
            with self.subTest(switched=switched):
                boss, source, sibling, outside, windows = self.sidebar()
                native_focus = [True]
                counters = {1: 1, 2: 2, 3: 3}

                def focus(os_window_id, focused, toggle=False):
                    before, native_focus[0] = native_focus[0], focused
                    return before

                with (
                    patch('kitty.boss.set_agents_sidebar_focus', side_effect=focus),
                    patch('kitty.boss.os_window_focus_counters', side_effect=lambda: dict(counters)),
                ):
                    boss.agents_sidebar_menu('1:10:close')
                    callback, overlay = boss.confirmations[0]
                    self.assertFalse(native_focus[0])
                    if switched == 'window':
                        for hook in overlay.actions_on_focus_change:
                            hook(overlay, False)
                    elif switched == 'os_window':
                        counters[1] += 1
                    elif switched == 'closed':
                        del boss.window_id_map[10]
                    elif switched == 'hidden':
                        boss.agent_sidebar_enabled = False
                    boss.active_window = windows[0]
                    callback(False)
                    self.ae(native_focus[0], switched == 'none')
                    self.ae(boss.closed, [])

    def test_sidebar_close_confirmation_revalidates_target_scope(self):
        from unittest.mock import patch

        for moved in (False, True):
            with self.subTest(moved=moved):
                boss, source, sibling, outside, windows = self.sidebar()
                with (
                    patch('kitty.boss.set_agents_sidebar_focus', return_value=True),
                    patch('kitty.boss.os_window_focus_counters', return_value={1: 1}),
                ):
                    boss.agents_sidebar_menu('1:10:close')
                    callback, overlay = boss.confirmations[0]
                    if moved:
                        source[0].remove(windows[0])
                        outside[0].append(windows[0])
                    callback(True)
                    self.ae(boss.closed, [] if moved else [windows[0]])


class TestAgentTimerLifecycle(BaseTest):
    def test_restored_sidebar_updates_unfocused_agent(self):
        from types import MethodType, SimpleNamespace
        from unittest.mock import patch

        from kitty.boss import Boss

        class Tab(list):
            sidebar_id = 'tab-1'
            effective_title = 'Agents'

        class Manager(list):
            os_window_id = 1
            agent_sidebar_enabled = False
            agent_sidebar_rows = ()

            def set_agent_sidebar_rows(self, rows):
                self.agent_sidebar_rows = tuple(rows)

        window = FakeWindow('\u2733 Thinking\u2026 (3s \u00b7 esc to interrupt)')
        tab = Tab([window])
        manager = Manager([tab])
        window.tabref = lambda: tab
        tab.tab_manager_ref = lambda: manager
        boss = SimpleNamespace(
            agent_sidebar_enabled=False, agent_sidebar_timer=0, agent_sidebar_flush_timer=0, _agent_debounce_timer=0,
            agent_tracker=AgentTracker(), window_id_map={window.id: window}, all_windows=(window,),
            all_tab_managers=(manager,), active_window=None, notify_agent_done=lambda snapshot: None,
            agent_sidebar_scope=lambda tm: (tm,),
        )
        for name in ('restore_agent_sidebar_state', 'refresh_agent_sidebars', 'flush_agent_sidebar_updates', '_flush_agent_sidebar_updates', '_refresh_agent_sidebar_scope'):
            setattr(boss, name, MethodType(getattr(Boss, name), boss))
        timers = []

        def add_timer(callback, delay, repeat=True):
            timers.append((callback, repeat))
            return len(timers)

        with (
            patch('kitty.boss.add_timer', side_effect=add_timer),
            patch('kitty.boss.get_options', return_value=SimpleNamespace(sidecar_grouping='tab')),
            patch('kitty.rc.agent_wait.wake_waiters'),
        ):
            boss.restore_agent_sidebar_state(SimpleNamespace(agent_sidebar_enabled=True, agent_sidebar_cols=30, agent_sidebar_collapsed_groups=()))
            boss.refresh_agent_sidebars()  # Initial population while the restored OS window is created.
            self.ae(manager.agent_sidebar_rows[0].state, AgentState.working)
            window.lines = ('\u256d\u2500\u2500\u256e', '\u2502 \u276f ', '\u2570\u2500\u2500\u256f')
            for timer_id, (callback, repeat) in enumerate(tuple(timers), 1):
                if repeat:
                    callback(timer_id)
            self.assertFalse(window.is_focused)
            self.ae(boss.agent_tracker.snapshot_for(window.id).state, AgentState.done)
            self.ae(manager.agent_sidebar_rows[0].state, AgentState.done)

    def test_agent_timers_are_released_on_shutdown(self):
        from contextlib import ExitStack
        from unittest.mock import patch

        from kitty.boss import Boss
        from kitty.cli import parse_args

        opts = self.set_options()
        args, _ = parse_args([])
        for debounce in (False, True):
            with self.subTest(debounce=debounce), ExitStack() as stack:
                timers = {}

                def add_timer(callback, delay, repeat):
                    timer_id = len(timers) + 1
                    timers[timer_id] = callback
                    return timer_id

                for name in ('ChildMonitor', 'set_boss', 'NotificationManager', 'destroy_global_data'):
                    stack.enter_context(patch(f'kitty.boss.{name}'))
                stack.enter_context(patch('kitty.boss.store_effective_config', return_value=('', '')))
                stack.enter_context(patch('kitty.boss.add_timer', side_effect=add_timer))
                stack.enter_context(patch('kitty.boss.remove_timer', side_effect=timers.pop))
                stack.enter_context(patch('kitty.agents.monotonic', return_value=100.1))
                boss = Boss(opts, args, {}, {})
                if debounce:
                    boss.agent_tracker.refresh_window(FakeWindow('\u2733 Thinking\u2026 (3s \u00b7 esc to interrupt)', focused=True), now=100.0)
                    boss.agent_tracker.refresh_window(FakeWindow('neutral text', focused=True), now=100.1)
                    boss.flush_agent_sidebar_updates()
                    self.assertIn(boss._agent_debounce_timer, timers)
                boss.destroy()
                self.ae(timers, {})
                self.assertTrue(boss.shutting_down)


class FakeWindow:
    id = 7
    title = 'Claude'

    def __init__(self, *lines: str, title: str = '', status=(), focused: bool = False):
        self.lines = lines
        self.osc_title = title
        self.status = list(status)
        self.is_focused = focused
        self.child = type('Child', (), {'cwd': '/tmp', 'foreground_processes': [{'cmdline': ['claude']}]})()
        self.program_status = type('S', (), {'as_list': lambda self_: self.status})()
        self.cwd_of_child = '/tmp'

    def agent_detection_snapshot(self):
        return Screen(self.lines, self.osc_title, '')


class TestAgentTracker(BaseTest):
    def test_tracker_caches_and_coalesces_windows(self):
        tracker = AgentTracker()
        self.assertTrue(tracker.refresh_window(FakeWindow('idle text')))
        self.assertFalse(tracker.refresh_window(FakeWindow('idle text')))
        tracker.mark_dirty(7)
        tracker.mark_dirty(7)
        self.ae(tracker.drain_dirty(), (7,))

    def test_discard_clears_all_window_state(self):
        tracker = AgentTracker()
        tracker.refresh_window(FakeWindow('idle text'))
        tracker.mark_dirty(7)
        tracker.discard(7)
        self.ae(tracker.drain_dirty(), ())
        self.assertIsNone(tracker.snapshot_for(7))

    def test_plain_idle_after_working_is_held_briefly(self):
        # Focused, so the finish does not turn into done and this isolates the
        # anti-flicker hold.
        tracker = AgentTracker()
        busy = FakeWindow('\u2733 Thinking\u2026 (3s \u00b7 esc to interrupt)', focused=True)
        tracker.refresh_window(busy, now=100.0)
        self.ae(tracker.snapshot_for(7).state, AgentState.working)
        # Mid-repaint: the working marker is gone but no idle marker yet.
        quiet = FakeWindow('neutral text', focused=True)
        tracker.refresh_window(quiet, now=100.1)
        self.ae(tracker.snapshot_for(7).state, AgentState.working)
        tracker.refresh_window(quiet, now=100.4)
        self.ae(tracker.snapshot_for(7).state, AgentState.idle)

    def test_visible_idle_evidence_applies_immediately(self):
        tracker = AgentTracker()
        tracker.refresh_window(FakeWindow('\u2733 Thinking\u2026 (3s \u00b7 esc to interrupt)', focused=True), now=100.0)
        self.ae(tracker.snapshot_for(7).state, AgentState.working)
        box = FakeWindow('\u256d\u2500\u2500\u256e', '\u2502 \u276f ', '\u2570\u2500\u2500\u256f', focused=True)
        tracker.refresh_window(box, now=100.05)
        self.ae(tracker.snapshot_for(7).state, AgentState.idle)

    def test_finished_turn_becomes_done_while_unfocused(self):
        tracker = AgentTracker()
        busy = FakeWindow('\u2733 Thinking\u2026 (3s \u00b7 esc to interrupt)')
        tracker.refresh_window(busy, now=100.0)
        self.ae(tracker.snapshot_for(7).state, AgentState.working)
        # Turn ends with the prompt box back. The user is looking elsewhere, so
        # the completion must be surfaced rather than quietly becoming idle.
        box = FakeWindow('\u256d\u2500\u2500\u256e', '\u2502 \u276f ', '\u2570\u2500\u2500\u256f')
        tracker.refresh_window(box, now=100.1)
        self.ae(tracker.snapshot_for(7).state, AgentState.done)
        # It stays done across refreshes until seen.
        tracker.refresh_window(box, now=101.0)
        self.ae(tracker.snapshot_for(7).state, AgentState.done)

    def test_done_clears_once_the_window_is_focused(self):
        tracker = AgentTracker()
        tracker.refresh_window(FakeWindow('\u2733 Thinking\u2026 (3s \u00b7 esc to interrupt)'), now=100.0)
        box = FakeWindow('\u256d\u2500\u2500\u256e', '\u2502 \u276f ', '\u2570\u2500\u2500\u256f')
        tracker.refresh_window(box, now=100.1)
        self.ae(tracker.snapshot_for(7).state, AgentState.done)
        seen = FakeWindow('\u256d\u2500\u2500\u256e', '\u2502 \u276f ', '\u2570\u2500\u2500\u256f', focused=True)
        tracker.refresh_window(seen, now=100.2)
        self.ae(tracker.snapshot_for(7).state, AgentState.idle)
        self.assertNotIn(7, tracker.unseen_done)

    def test_finishing_while_focused_never_shows_done(self):
        # The user is watching the turn end, so there is nothing to catch up on.
        tracker = AgentTracker()
        tracker.refresh_window(FakeWindow('\u2733 Thinking\u2026 (3s \u00b7 esc to interrupt)', focused=True), now=100.0)
        box = FakeWindow('\u256d\u2500\u2500\u256e', '\u2502 \u276f ', '\u2570\u2500\u2500\u256f', focused=True)
        tracker.refresh_window(box, now=100.1)
        self.ae(tracker.snapshot_for(7).state, AgentState.idle)

    def test_new_turn_supersedes_an_unseen_completion(self):
        tracker = AgentTracker()
        busy = FakeWindow('\u2733 Thinking\u2026 (3s \u00b7 esc to interrupt)')
        tracker.refresh_window(busy, now=100.0)
        box = FakeWindow('\u256d\u2500\u2500\u256e', '\u2502 \u276f ', '\u2570\u2500\u2500\u256f')
        tracker.refresh_window(box, now=100.1)
        self.ae(tracker.snapshot_for(7).state, AgentState.done)
        # Working again: the completion is no longer the latest thing.
        tracker.refresh_window(busy, now=100.2)
        self.ae(tracker.snapshot_for(7).state, AgentState.working)
        self.assertNotIn(7, tracker.unseen_done)

    def test_omp_question_wait_does_not_become_unseen_done(self):
        for initial in (AgentState.working, AgentState.done):
            with self.subTest(initial=initial):
                tracker = AgentTracker()
                window = FakeWindow(title='π ⠙ tmp')
                window.child.foreground_processes = [{'cmdline': ['omp']}]
                tracker.refresh_window(window, now=100.0)
                if initial is AgentState.done:
                    window.osc_title = 'π > tmp'
                    tracker.refresh_window(window, now=100.1)
                self.ae(tracker.snapshot_for(7).state, initial)
                window.lines = ('Ask hidden: Status probe · ⌥H/⏎ show · ⎋ composer',)
                window.osc_title = 'π > tmp'
                tracker.refresh_window(window, now=100.2)
                self.ae(tracker.snapshot_for(7).state, AgentState.blocked)
                self.assertNotIn(7, tracker.unseen_done)
                window.lines = ()
                window.osc_title = 'π ⠙ tmp'
                tracker.refresh_window(window, now=100.3)
                self.ae(tracker.snapshot_for(7).state, AgentState.working)
                window.osc_title = 'π > tmp'
                tracker.refresh_window(window, now=100.4)
                self.ae(tracker.snapshot_for(7).state, AgentState.done)

    def test_live_snapshot_reuses_only_unchanged_screen_evidence(self):
        from unittest.mock import Mock

        from kitty.window import Window

        window = FakeWindow(focused=True)
        window.screen = self.create_screen(cols=80, lines=8)
        window.screen.draw('idle text')
        window.progress_payload = ''
        window.agent_detection_snapshot = Mock(side_effect=lambda: Window.agent_detection_snapshot(window))
        tracker = AgentTracker()
        self.ae(tracker.live_snapshot(window)[0].state, AgentState.idle)
        tracker.live_snapshot(window)
        self.ae(window.agent_detection_snapshot.call_count, 1)
        window.screen.draw('\r\u2733 Thinking\u2026 (3s \u00b7 esc to interrupt)')
        self.ae(tracker.live_snapshot(window)[0].state, AgentState.working)
        self.ae(window.agent_detection_snapshot.call_count, 2)
        window.title = 'Changed title'
        tracker.live_snapshot(window)
        window.progress_payload = 'working'
        tracker.live_snapshot(window)
        self.ae(window.agent_detection_snapshot.call_count, 4)
        replacement = self.create_screen(cols=80, lines=8)
        replacement.draw('idle text')
        window.screen = replacement
        self.ae(tracker.live_snapshot(window)[0].state, AgentState.idle)
        self.ae(window.agent_detection_snapshot.call_count, 5)
        window.child.foreground_processes = [{'cmdline': ['/bin/zsh']}]
        self.assertIsNone(tracker.live_snapshot(window)[0])
        self.ae(window.agent_detection_snapshot.call_count, 5)
        self.assertNotIn(window.id, tracker.detection_cache)

    def test_live_snapshot_keeps_service_state_separate_from_done_hold(self):
        from types import SimpleNamespace

        from kitty.agent_service import AgentService

        window = FakeWindow('\u2733 Thinking\u2026 (3s \u00b7 esc to interrupt)')
        tracker = AgentTracker()
        tracker.refresh_window(window, now=1.)
        window.lines = ('\u256d\u2500\u2500\u256e', '\u2502 \u276f ', '\u2570\u2500\u2500\u256f')
        tracker.refresh_window(window, now=2.)
        self.ae(tracker.snapshot_for(window.id).state, AgentState.done)
        service = AgentService(SimpleNamespace(agent_tracker=tracker))
        self.ae(service.observe(window)['state'], 'idle')
        self.ae(tracker.snapshot_for(window.id).state, AgentState.done)

    def test_opencode_native_status_prevents_false_completion(self):
        from unittest.mock import Mock

        window = FakeWindow('esc to interrupt')
        window.child.foreground_processes = [{'cmdline': ['opencode']}]
        tracker = AgentTracker()
        tracker.refresh_window(window, now=100.0)
        self.ae(tracker.snapshot_for(window.id).state, AgentState.working)
        window.lines = ('Orchestrator · gpt-6.1-sol',)
        window.status = [
            {'id': '', 'app': 'opencode', 'state': 'working'},
            {'id': 'ses_active', 'app': 'opencode', 'state': 'working'},
        ]
        window.agent_detection_snapshot = Mock(side_effect=AssertionError('Native status must not need screen detection'))
        tracker.refresh_window(window, now=101.0)
        tracker.refresh_window(window, now=102.0)
        self.ae(tracker.snapshot_for(window.id).state, AgentState.working)
        self.assertNotIn(window.id, tracker.unseen_done)
        window.status = [{'id': '', 'app': 'opencode', 'state': 'idle'}]
        tracker.refresh_window(window, now=103.0)
        self.ae(tracker.snapshot_for(window.id).state, AgentState.done)
        window.status = [{'id': '', 'app': 'opencode', 'state': 'working'}]
        tracker.refresh_window(window, now=104.0)
        self.ae(tracker.snapshot_for(window.id).state, AgentState.working)
        self.assertNotIn(window.id, tracker.unseen_done)

    def test_full_lifecycle_without_usable_report_still_reads_screen(self):
        from unittest.mock import Mock

        window = FakeWindow('idle text')
        window.child.foreground_processes = [{'cmdline': ['opencode']}]
        window.program_status = type('Status', (), {'as_list': lambda _: [{'state': 'working', 'id': 'unrelated'}]})()
        window.agent_detection_snapshot = Mock(return_value=Screen(('idle text',), '', ''))
        snapshot = AgentTracker().live_snapshot(window)[0]
        self.assertIsNotNone(snapshot)
        window.agent_detection_snapshot.assert_called_once()

    def test_live_snapshot_tracks_live_bottom_through_history_and_alternate(self):
        from unittest.mock import Mock

        from kitty.window import Window

        from .base import parse_bytes

        window = FakeWindow(focused=True)
        window.screen = self.create_screen(cols=80, lines=8, scrollback=100)
        window.progress_payload = ''
        window.agent_detection_snapshot = Mock(side_effect=lambda: Window.agent_detection_snapshot(window))
        window.screen.draw(('old history\r\n' * 12) + '\u2733 Thinking\u2026 (3s \u00b7 esc to interrupt)')
        tracker = AgentTracker()
        self.ae(tracker.live_snapshot(window)[0].state, AgentState.working)
        window.screen.scroll(4, True)
        self.ae(tracker.live_snapshot(window)[0].state, AgentState.working)
        self.ae(window.agent_detection_snapshot.call_count, 1)
        parse_bytes(window.screen, b'\x1b[?1049h')
        window.screen.draw('alternate idle')
        self.ae(tracker.live_snapshot(window)[0].state, AgentState.idle)
        parse_bytes(window.screen, b'\x1b[?1049l')
        self.ae(tracker.live_snapshot(window)[0].state, AgentState.working)


class TestAgentResume(BaseTest):
    SESSION_ID = '019a90db-05e8-7c50-af14-8425b9d1c3a9'

    def window(self, **user_vars: str) -> FakeWindow:
        w = FakeWindow('idle text')
        w.user_vars = user_vars
        return w

    def test_reported_session_id_is_used_only_when_it_is_usable(self):
        from kitty.agents import AGENT_ID_VAR, AGENT_SESSION_VAR, session_id_of_window

        # An agent that reports which agent it is must agree with the agent actually running, including by alias
        self.ae(session_id_of_window(self.window(**{AGENT_SESSION_VAR: self.SESSION_ID}), 'claude'), self.SESSION_ID)
        for reported in ('claude', 'claude-code'):
            w = self.window(**{AGENT_SESSION_VAR: self.SESSION_ID, AGENT_ID_VAR: reported})
            self.ae(session_id_of_window(w, 'claude'), self.SESSION_ID)
        for reported in ('codex', 'nonexistent-agent'):
            w = self.window(**{AGENT_SESSION_VAR: self.SESSION_ID, AGENT_ID_VAR: reported})
            self.ae(session_id_of_window(w, 'claude'), '')
        # A window with no agent reporting has nothing to resume
        self.ae(session_id_of_window(self.window(), 'claude'), '')

    def test_untrusted_session_ids_are_rejected(self):
        from kitty.agents import AGENT_SESSION_VAR, session_id_of_window

        # The id arrives over an escape code from the window, so anything that could act as an option, a path or shell syntax when
        # it reaches a command line is discarded rather than sanitized
        for bad in ('--resume', '-r', '/etc/passwd', '../../etc/passwd', 'a;id', 'a b', '$(id)', '`id`', 'a|b', '.hidden', 'x' * 129):
            self.ae(session_id_of_window(self.window(**{AGENT_SESSION_VAR: bad}), 'claude'), '', f'accepted {bad!r}')
        for good in ('abc', 'A1', self.SESSION_ID, 'name_with.dots-and_dashes', 'x' * 128):
            self.ae(session_id_of_window(self.window(**{AGENT_SESSION_VAR: good}), 'claude'), good, f'rejected {good!r}')

    def test_resume_command_comes_from_the_manifest(self):
        from kitty.agents import resume_argv_for

        # Each argument but the id is manifest data, so the agent chooses the conversation, never the command
        self.ae(resume_argv_for('claude', self.SESSION_ID), ('claude', '--resume', self.SESSION_ID))
        self.ae(resume_argv_for('codex', self.SESSION_ID), ('codex', 'resume', self.SESSION_ID))
        # Agents whose manifest has no resume command, and unknown agents, cannot be resumed
        self.ae(resume_argv_for('gemini', self.SESSION_ID), ())
        self.ae(resume_argv_for('nonexistent-agent', self.SESSION_ID), ())
        self.ae(resume_argv_for('claude', ''), ())

    def test_manifest_resume_specs_are_validated(self):

        from kitty.agent_manifest import parse_manifest

        def manifest(resume: dict) -> object:
            return parse_manifest({'id': 'x', 'version': '1', 'resume': resume, 'rules': []}, 'test')

        self.ae(manifest({'argv': ['x', '--resume', '{session}']}).resume.argv, ('x', '--resume', '{session}'))
        # A spec that cannot produce exactly one session id, or that runs something other than a command name, is a broken
        # manifest rather than something to work around at restore time
        for bad in ({'argv': []}, {'argv': ['x']}, {'argv': ['x', '{session}', '{session}']}, {'argv': ['{session}']}, {'argv': ['./x', '{session}']}):
            with self.assertRaises(ValueError):
                manifest(bad)

    def test_herdr_native_resume_matrix(self):
        from kitty.agent_manifest import manifest_for

        expected = {
            'pi': ('pi', '--session', '{session}'),
            'omp': ('omp', '--resume={session}'),
            'claude': ('claude', '--resume', '{session}'),
            'codex': ('codex', 'resume', '{session}'),
            'copilot': ('copilot', '--resume={session}'),
            'devin': ('devin', '--resume', '{session}'),
            'droid': ('droid', '--resume', '{session}'),
            'kimi': ('kimi', '--session', '{session}'),
            'opencode': ('opencode', '--session', '{session}'),
            'kilo': ('kilo', '--session', '{session}'),
            'hermes': ('hermes', '--resume', '{session}'),
            'qodercli': ('qodercli', '--resume', '{session}'),
            'qwen': ('qwen', '--resume', '{session}'),
            'letta': ('letta', '--conversation', '{session}'),
            'cursor': ('cursor-agent', '--resume', '{session}'),
            'mastracode': ('mastracode', '--thread', '{session}'),
            'agy': ('agy', '--conversation', '{session}'),
            'grok': ('grok', '--resume', '{session}'),
        }
        for agent, argv in expected.items():
            manifest = manifest_for(agent)
            self.assertIsNotNone(manifest, agent)
            self.ae(manifest.resume.argv, argv, agent)

    def test_snapshot_carries_the_session_id(self):
        from kitty.agents import AGENT_SESSION_VAR, snapshot_for_window

        snapshot, _ = snapshot_for_window(self.window(**{AGENT_SESSION_VAR: self.SESSION_ID}))
        self.ae((snapshot.agent, snapshot.session_id), ('claude', self.SESSION_ID))
        snapshot, _ = snapshot_for_window(self.window())
        self.ae(snapshot.session_id, '')

    def test_pi_omp_accept_session_paths(self):
        from kitty.agents import AGENT_ID_VAR, AGENT_SESSION_PATH_VAR, resume_argv_for, session_ref_of_window

        path = '/Users/test/.omp/sessions/abc.jsonl'
        w = self.window(**{AGENT_ID_VAR: 'omp', AGENT_SESSION_PATH_VAR: path})
        self.ae(session_ref_of_window(w, 'omp'), path)
        self.ae(resume_argv_for('omp', path), ('omp', f'--resume={path}'))
        self.ae(resume_argv_for('pi', path), ('pi', '--session', path))
        self.ae(resume_argv_for('claude', path), ())
        self.ae(session_ref_of_window(self.window(**{AGENT_SESSION_PATH_VAR: 'relative/path'}), 'omp'), '')

    def test_herdr_integration_capabilities(self):
        from kitty.agent_integration import AGENTS

        agents = {agent.id: agent for agent in AGENTS}
        self.ae(agents['claude'].integration_kind, 'hook')
        self.ae(agents['codex'].integration_kind, 'hook')
        for agent in ('pi', 'omp'):
            self.ae(agents[agent].integration_kind, 'native')
        self.ae(agents['opencode'].integration_kind, 'tui-plugin')
        for agent in ('kilo', 'hermes'):
            self.ae(agents[agent].integration_kind, 'plugin-required')
        for agent in ('kimi',):
            self.ae(agents[agent].integration_kind, 'adapter-required')
        for agent in ('copilot', 'devin', 'droid', 'qodercli', 'qwen', 'letta', 'cursor', 'grok', 'agy', 'mastracode'):
            self.ae(agents[agent].integration_kind, 'adapter-required')

    def test_herdr_source_identity(self):
        from kitty.agents import AGENT_SOURCE_VAR, session_ref_of_window

        sid = self.SESSION_ID
        self.ae(session_ref_of_window(self.window(KITTY_AGENT_SESSION=sid), 'claude'), sid)
        self.ae(session_ref_of_window(self.window(**{AGENT_SOURCE_VAR: 'kitty:claude', 'KITTY_AGENT_SESSION': sid}), 'claude'), sid)
        self.ae(session_ref_of_window(self.window(**{AGENT_SOURCE_VAR: 'herdr:claude', 'KITTY_AGENT_SESSION': sid}), 'claude'), sid)
        self.ae(session_ref_of_window(self.window(**{AGENT_SOURCE_VAR: 'herdr:codex', 'KITTY_AGENT_SESSION': sid}), 'claude'), '')
