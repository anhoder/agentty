#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

import json
import os
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

from kitty import agent_integration as integration

from .base import BaseTest


class TestAgentIntegration(BaseTest):
    @contextmanager
    def isolated_agent(self, agent_id, create_config=False):
        with tempfile.TemporaryDirectory() as root:
            env = {
                'HOME': root,
                'CLAUDE_CONFIG_DIR': os.path.join(root, 'claude'),
                'CODEX_HOME': os.path.join(root, 'codex'),
                'COPILOT_HOME': os.path.join(root, 'copilot'),
                'PI_CODING_AGENT_DIR': os.path.join(root, agent_id, 'agent'),
                'XDG_CONFIG_HOME': os.path.join(root, 'config'),
            }
            with (
                patch.object(integration, 'config_dir', os.path.join(root, 'kitty')),
                patch.dict(os.environ, env),
            ):
                agent = integration.agent_by_id(agent_id)
                config_path = agent.config_path()
                if create_config:
                    os.makedirs(os.path.dirname(config_path), exist_ok=True)
                yield root, agent, config_path

    @staticmethod
    def files(root):
        ans = {}
        for base, dirs, names in os.walk(root):
            for name in dirs:
                path = os.path.join(base, name)
                ans[os.path.relpath(path, root)] = ('directory', os.stat(path).st_mode)
            for name in names:
                path = os.path.join(base, name)
                with open(path, 'rb') as f:
                    ans[os.path.relpath(path, root)] = ('file', os.stat(path).st_mode, f.read())
        return ans

    def inspect_state(self, root, agent, state):
        before = self.files(root)
        inspection = integration.inspect_integration(agent)
        self.assertEqual(inspection.state, state, inspection.detail)
        self.assertEqual(self.files(root), before)
        return inspection

    def test_agent_integration_missing_current_and_outdated(self):
        for agent_id in ('claude', 'codex', 'pi', 'omp', 'opencode'):
            with self.subTest(agent=agent_id), self.isolated_agent(agent_id) as (root, agent, config_path):
                self.inspect_state(root, agent, 'missing')
                os.makedirs(os.path.dirname(config_path), exist_ok=True)
                missing = self.inspect_state(root, agent, 'missing')
                self.assertTrue(missing.paths)

                integration.install(agent.id)
                current = self.inspect_state(root, agent, 'current')
                self.assertTrue(all(os.path.isfile(path) for path in current.paths))
                asset_path = current.paths[-1]
                with open(asset_path, 'rb') as f:
                    original = f.read()
                self.assertTrue(original)
                with open(asset_path, 'ab') as f:
                    f.write(b'\n')
                self.inspect_state(root, agent, 'outdated')

                integration.install(agent.id)
                self.inspect_state(root, agent, 'current')
                with open(asset_path, 'rb') as f:
                    self.assertEqual(f.read(), original)

    def test_agent_integration_hook_registration_repair(self):
        for agent_id in ('claude', 'codex'):
            with self.subTest(agent=agent_id), self.isolated_agent(agent_id, create_config=True) as (root, agent, config_path):
                original = {
                    'permissions': {'allow': ['Read(*)']},
                    'hooks': {
                        'SessionStart': [
                            {
                                'matcher': 'startup',
                                'hooks': [
                                    {'type': 'command', 'command': 'echo kitty-keep-me', 'timeout': 3},
                                ],
                            }
                        ],
                        'PreToolUse': [{'hooks': [{'type': 'command', 'command': 'echo other-event'}]}],
                    },
                }
                with open(config_path, 'w') as f:
                    json.dump(original, f)
                integration.install(agent.id)
                current = self.inspect_state(root, agent, 'current')
                with open(current.paths[-1], 'rb') as f:
                    script = f.read()

                with open(config_path, 'w') as f:
                    json.dump(original, f)
                self.inspect_state(root, agent, 'outdated')
                integration.install(agent.id)
                self.inspect_state(root, agent, 'current')
                with open(current.paths[-1], 'rb') as f:
                    self.assertEqual(f.read(), script)
                with open(config_path) as f:
                    repaired = json.load(f)
                self.assertEqual(repaired['permissions'], original['permissions'])
                self.assertEqual(repaired['hooks']['PreToolUse'], original['hooks']['PreToolUse'])
                self.assertEqual(repaired['hooks']['SessionStart'][0], original['hooks']['SessionStart'][0])
                self.assertEqual(len(repaired['hooks']['SessionStart']), 2)

    def test_agent_integration_plugin_registration_repair(self):
        with self.isolated_agent('opencode', create_config=True) as (root, agent, config_path):
            missing = self.inspect_state(root, agent, 'missing')
            plugin_config = missing.paths[0]
            original = {'theme': 'dark', 'plugin': ['other-plugin.js', ['configured-plugin', {'enabled': True}]]}
            with open(plugin_config, 'w') as f:
                json.dump(original, f)
            self.inspect_state(root, agent, 'missing')
            integration.install(agent.id)
            self.inspect_state(root, agent, 'current')
            with open(plugin_config) as f:
                installed = json.load(f)
            self.assertEqual(installed['theme'], original['theme'])
            self.assertEqual(installed['plugin'][:-1], original['plugin'])
            self.assertEqual(len(installed['plugin']), len(original['plugin']) + 1)

            with open(plugin_config, 'w') as f:
                json.dump(original, f)
            self.inspect_state(root, agent, 'outdated')
            integration.install(agent.id)
            self.inspect_state(root, agent, 'current')
            with open(plugin_config) as f:
                self.assertEqual(json.load(f), installed)
            integration.install(agent.id)
            with open(plugin_config) as f:
                self.assertEqual(json.load(f), installed)

    def test_agent_integration_conflict_preflight(self):
        for agent_id in ('claude', 'codex', 'pi', 'omp', 'opencode'):
            with self.subTest(agent=agent_id), self.isolated_agent(agent_id, create_config=True) as (root, agent, config_path):
                missing = self.inspect_state(root, agent, 'missing')
                if agent.integration_kind == 'hook':
                    with open(config_path, 'w') as f:
                        json.dump({'hooks': {'SessionStart': []}}, f)
                elif agent.integration_kind == 'tui-plugin':
                    with open(missing.paths[0], 'w') as f:
                        json.dump({'plugin': ['other-plugin.js']}, f)
                asset_path = missing.paths[-1]
                os.makedirs(os.path.dirname(asset_path), exist_ok=True)
                with open(asset_path, 'w') as f:
                    f.write('user-owned integration\n')
                before = self.files(root)
                self.inspect_state(root, agent, 'conflict')
                with self.assertRaises(SystemExit):
                    integration.install(agent.id)
                self.assertEqual(self.files(root), before)

    def test_agent_integration_invalid_config_preflight(self):
        for agent_id, contents in (
            ('claude', '{invalid JSON'),
            ('claude', '{"hooks": []}'),
            ('codex', '{"hooks": {"SessionStart": {}}}'),
            ('opencode', '{invalid JSON'),
            ('opencode', '{"plugin": {}}'),
        ):
            with self.subTest(agent=agent_id, contents=contents), self.isolated_agent(agent_id, create_config=True) as (root, agent, config_path):
                missing = self.inspect_state(root, agent, 'missing')
                with open(missing.paths[0], 'w') as f:
                    f.write(contents)
                before = self.files(root)
                self.inspect_state(root, agent, 'error')
                with self.assertRaises(SystemExit):
                    integration.install(agent.id)
                self.assertEqual(self.files(root), before)

    def test_agent_integration_unsupported(self):
        with self.isolated_agent('copilot', create_config=True) as (root, agent, config_path):
            with open(config_path, 'w') as f:
                json.dump({'theme': 'unchanged'}, f)
            before = self.files(root)
            self.inspect_state(root, agent, 'unsupported')
            with self.assertRaises(SystemExit):
                integration.install(agent.id)
            self.assertEqual(self.files(root), before)
