#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

import os
from types import SimpleNamespace
from unittest.mock import patch

from kitty.agent_integration import IntegrationInspection
from kitty.agent_onboarding import AgentIntegrationPrompts

from .base import BaseTest


class TestAgentOnboarding(BaseTest):
    def setup_prompt(self, state='missing'):
        environment = {key: os.environ[key] for key in ('HOME', 'XDG_CONFIG_HOME', 'CLAUDE_CONFIG_DIR', 'CODEX_HOME', 'PI_CODING_AGENT_DIR', 'OPENCODE_CONFIG_DIR') if key in os.environ}
        window = SimpleNamespace(id=1, is_focused=True, child_is_remote=False, ssh_kitten_cmdline=lambda: [],
                                 child=SimpleNamespace(foreground_processes=[{'cmdline': ['opencode']}],
                                                       foreground_environ=environment.copy(), environ=environment.copy()))
        self.calls = []
        self.notifications = []
        self.errors = []

        def choose(message, callback, *choices, **kwargs):
            self.calls.append((message, callback, choices))
            return SimpleNamespace()

        boss = SimpleNamespace(shutting_down=False, active_window=window, window_id_map={1: window}, cached_values={},
                               choose=choose, show_error=lambda *args: self.errors.append(args),
                               notification_manager=SimpleNamespace(create_notification_cmd=lambda: SimpleNamespace(),
                                   notify_with_command=lambda cmd, wid: self.notifications.append(cmd)))
        self.prompts = AgentIntegrationPrompts(boss)
        self.inspection = IntegrationInspection(state, ('/tmp/opencode/config', '/tmp/opencode/reporter'), '')
        return boss, window

    def test_no_install_before_consent_and_later_deduplicates(self):
        self.setup_prompt()
        with patch('kitty.agent_onboarding.inspect_integration', return_value=self.inspection), patch('kitty.agent_onboarding.install') as install:
            self.prompts.poll()
            self.prompts.poll()
            self.ae(len(self.calls), 1)
            install.assert_not_called()
            self.calls[0][1]('l')
            self.prompts.poll()
            self.ae(len(self.calls), 1)
            install.assert_not_called()

    def test_never_choice_survives_controller_restart(self):
        boss, _ = self.setup_prompt()
        with patch('kitty.agent_onboarding.inspect_integration', return_value=self.inspection):
            self.prompts.poll()
            self.calls[0][1]('n')
            AgentIntegrationPrompts(boss).poll()
            self.ae(len(self.calls), 1)
            self.assertIn('opencode', boss.cached_values['agent-integration-never-prompt'])

    def test_update_requires_consent_and_reports_restart(self):
        self.setup_prompt('outdated')
        with patch('kitty.agent_onboarding.inspect_integration', return_value=self.inspection), patch('kitty.agent_onboarding.install') as install:
            self.prompts.poll()
            self.assertIn('Update', self.calls[0][2][0])
            install.assert_not_called()
            self.calls[0][1]('u')
            install.assert_called_once_with('opencode')
            self.assertIn('Restart', self.notifications[0].body)

    def test_process_title_rewrite_does_not_block_prompt_or_install(self):
        _, window = self.setup_prompt()
        window.child.foreground_processes = [{'cmdline': ['pi', '', '']}]
        window.child.foreground_environ = {}
        window.child.environ = window.child.environ.copy()
        with patch('kitty.agent_onboarding.inspect_integration', return_value=self.inspection), patch('kitty.agent_onboarding.install') as install:
            self.prompts.poll()
            self.ae(len(self.calls), 1)
            self.calls[0][1]('i')
            install.assert_called_once_with('pi')

    def test_remote_background_and_different_home_do_not_prompt(self):
        _, window = self.setup_prompt()
        with patch('kitty.agent_onboarding.inspect_integration') as inspect:
            window.is_focused = False
            self.prompts.poll()
            window.is_focused = True
            window.child_is_remote = True
            self.prompts.poll()
            window.child_is_remote = False
            window.child.foreground_environ['HOME'] = '/different/home'
            self.prompts.poll()
            inspect.assert_not_called()
            self.ae(self.calls, [])

    def test_conflict_current_and_unsupported_agents_do_not_prompt(self):
        _, window = self.setup_prompt()
        for state in ('conflict', 'current', 'error'):
            with patch('kitty.agent_onboarding.inspect_integration', return_value=IntegrationInspection(state, (), '')):
                self.prompts.checked.clear()
                self.prompts.poll()
        window.child.foreground_processes = [{'cmdline': ['copilot']}]
        self.prompts.poll()
        self.ae(self.calls, [])

    def test_changed_target_cannot_install_and_install_error_is_visible(self):
        _, window = self.setup_prompt()
        with patch('kitty.agent_onboarding.inspect_integration', return_value=self.inspection), patch('kitty.agent_onboarding.install') as install:
            self.prompts.poll()
            window.child.foreground_processes = [{'cmdline': ['claude']}]
            self.calls[0][1]('i')
            install.assert_not_called()
        self.setup_prompt()
        with patch('kitty.agent_onboarding.inspect_integration', return_value=self.inspection), patch('kitty.agent_onboarding.install', side_effect=SystemExit('unowned reporter')):
            self.prompts.poll()
            self.calls[0][1]('i')
            self.ae(self.errors[0][1], 'unowned reporter')
            self.ae(self.notifications, [])
