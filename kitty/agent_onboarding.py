#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

"""Consent-based installation prompts for local agents in focused windows."""

import os
from gettext import gettext as _
from typing import Any, Mapping

from .agent_integration import AGENTS, Agent, inspect_integration, install
from .agents import detect_agent
from .child import cached_process_data, environ_of_process


def _config_home(env: Mapping[str, str], name: str, default: str) -> str:
    # A shell can export a variable (for example XDG_CONFIG_HOME in fish's
    # config.fish) after Kitty launched it, so Kitty's own environment may lack
    # a value that resolves to the very same directory. Compare the resolved
    # directories instead of the raw values.
    return os.path.abspath(os.path.expanduser(env.get(name) or default))


class AgentIntegrationPrompts:
    def __init__(self, boss: Any) -> None:
        self.boss = boss
        self.checked: set[tuple[str, str]] = set()
        self.prompt_open = False

    @staticmethod
    def local_environment(agent: Agent, window: Any) -> bool:
        env = {}
        agent_id = detect_agent(window.child.foreground_processes)
        for process in window.child.foreground_processes:
            if detect_agent((process,)) == agent_id and process.get('pid'):
                try:
                    env = environ_of_process(process['pid'])
                except (OSError, ValueError):
                    pass
                break
        # Node's process.title can overwrite the argv/environ memory on macOS.
        # Child.environ already falls back to the launch environment when the
        # process environment cannot be read. Do not confuse that with a remote
        # or differently configured agent.
        env = env or window.child.foreground_environ or window.child.environ
        if env.get('HOME') != os.environ.get('HOME'):
            return False
        config_env = agent.config_env
        if config_env is not None:
            name, default = config_env
            if _config_home(env, name, default) != _config_home(os.environ, name, default):
                return False
        return not (env.get('SSH_CONNECTION') or env.get('SSH_TTY'))

    def poll(self, timer_id: int | None = None) -> None:
        boss = self.boss
        if boss.shutting_down or self.prompt_open:
            return
        window = boss.active_window
        if window is None or not window.is_focused:
            return
        with cached_process_data():
            if window.child_is_remote or window.ssh_kitten_cmdline():
                return
            agent_id = detect_agent(window.child.foreground_processes)
        agent = next((a for a in AGENTS if a.id == agent_id), None)
        if agent is None or agent.integration_kind not in ('hook', 'native', 'tui-plugin'):
            return
        if agent.id in boss.cached_values.get('agent-integration-never-prompt', []):
            return
        key = (agent.id, agent.config_path())
        if key in self.checked:
            return
        # Do not install into Kitty's config home when this agent uses another
        # home or is itself running inside a remote/container environment.
        if not self.local_environment(agent, window):
            return
        self.checked.add(key)
        inspection = inspect_integration(agent)
        if inspection.state not in ('missing', 'outdated'):
            return
        updating = inspection.state == 'outdated'
        action = _('Update') if updating else _('Install')
        message = _('Update the Kitty integration for {agent}?') if updating else _('Enable the Kitty integration for {agent}?')
        message = message.format(agent=agent.name)
        message += '\n\n' + _('Reports supported agent activity and identifies conversations for session restore.')
        message += '\n\n' + _('Files that may change:') + '\n' + '\n'.join(inspection.paths)
        message += '\n\n' + _('Restart the agent after installation. This will not restart it or send it any input.')
        self.prompt_open = True

        def answered(choice: str) -> None:
            self.prompt_open = False
            if choice == 'n':
                dismissed = list(boss.cached_values.get('agent-integration-never-prompt', []))
                if agent.id not in dismissed:
                    dismissed.append(agent.id)
                boss.cached_values['agent-integration-never-prompt'] = dismissed
            elif choice in ('i', 'u'):
                target = boss.window_id_map.get(window.id)
                if (
                    target is not window
                    or target.child_is_remote
                    or target.ssh_kitten_cmdline()
                    or detect_agent(target.child.foreground_processes) != agent.id
                    or not self.local_environment(agent, target)
                ):
                    return
                try:
                    # install() repeats ownership/config validation at consent time.
                    install(agent.id)
                except (Exception, SystemExit) as err:
                    boss.show_error(_('Agent integration installation failed'), str(err))
                    return
                cmd = boss.notification_manager.create_notification_cmd()
                cmd.title = _('{agent} integration installed').format(agent=agent.name)
                cmd.body = _('Restart {agent} to load it. The running process has not been changed.').format(agent=agent.name)
                boss.notification_manager.notify_with_command(cmd, window.id)

        try:
            key = 'u' if updating else 'i'
            overlay = boss.choose(
                message,
                answered,
                f'{key}:{action}',
                'l:' + _('Later'),
                'n:' + _('Never ask again'),
                window=window,
                default='l',
                title=_('Kitty agent integration'),
            )
            if overlay is None:
                self.prompt_open = False
        except Exception:
            self.prompt_open = False
            raise
