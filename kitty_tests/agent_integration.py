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
            original = {'theme': 'dark', 'plugins': ['other-plugin', ['configured-plugin', {'enabled': True}]], 'plugin': ['unrelated']}
            with open(plugin_config, 'w') as f:
                json.dump(original, f)
            self.inspect_state(root, agent, 'missing')
            integration.install(agent.id)
            self.inspect_state(root, agent, 'current')
            with open(plugin_config) as f:
                installed = json.load(f)
            self.assertEqual(installed['theme'], original['theme'])
            self.assertEqual(installed['plugin'], original['plugin'])
            self.assertEqual(installed['plugins'], original['plugins'] + ['./kitty-agent-state'])
            self.assertEqual(config_path, plugin_config)
            self.assertNotIn('$schema', installed)
            self.assertEqual(integration.reporter_path(agent), os.path.join(os.path.dirname(config_path), 'kitty-agent-state', 'tui.js'))
            self.assertTrue(integration.reporter_is_installed(agent))

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

    def test_agent_integration_plugin_jsonc_precedence(self):
        with self.isolated_agent('opencode', create_config=True) as (root, agent, config_path):
            fallback = '{"plugins": ["fallback"], "other": true}\n'
            with open(config_path, 'w') as f:
                f.write(fallback)
            active = config_path + 'c'
            with open(active, 'w') as f:
                f.write('// active config\n{"plugins": ["active",], "theme": "dark",}\n')
            self.assertEqual(integration.opencode_plugin_config(agent), (active, 'plugins'))
            integration.install(agent.id)
            self.assertEqual(self.inspect_state(root, agent, 'current').paths[0], active)
            installed = integration.read_plugin_config(active)
            self.assertEqual(installed, {'plugins': ['active', './kitty-agent-state'], 'theme': 'dark'})
            with open(config_path) as f:
                self.assertEqual(f.read(), fallback)
            before = self.files(root)
            integration.install(agent.id)
            self.assertEqual(self.files(root), before)
            integration.uninstall(agent.id)
            self.assertEqual(integration.read_plugin_config(active), {'plugins': ['active'], 'theme': 'dark'})
            self.assertFalse(integration.reporter_is_installed(agent))
            with open(config_path) as f:
                self.assertEqual(f.read(), fallback)
            before = self.files(root)
            integration.uninstall(agent.id)
            self.assertEqual(self.files(root), before)

    def test_agent_integration_plugin_legacy_migration(self):
        for legacy_file in (False, True):
            with self.subTest(legacy_file=legacy_file), self.isolated_agent('opencode', create_config=True) as (root, agent, config_path):
                base = os.path.dirname(config_path)
                legacy = os.path.join(base, 'kitty-agent-state.js')
                if legacy_file:
                    with open(legacy, 'w') as f:
                        f.write('// ' + integration.KITTY_ASSET_MARKER + '\nlegacy reporter\n')
                original = {'plugin': ['other.js', './kitty-agent-state.js', ['configured', {'enabled': True}]], 'theme': 'dark'}
                for filename in ('tui.json', 'tui.jsonc'):
                    with open(os.path.join(base, filename), 'w') as f:
                        json.dump(original, f)
                unrelated = os.path.join(base, 'other.js')
                with open(unrelated, 'w') as f:
                    f.write('user plugin\n')
                self.inspect_state(root, agent, 'outdated' if legacy_file else 'missing')
                integration.install(agent.id)
                self.inspect_state(root, agent, 'current')
                self.assertFalse(os.path.lexists(legacy))
                for filename in ('tui.json', 'tui.jsonc'):
                    self.assertEqual(integration.read_plugin_config(os.path.join(base, filename)), {
                        'plugin': ['other.js', ['configured', {'enabled': True}]], 'theme': 'dark',
                    })
                self.assertEqual(integration.read_plugin_config(config_path)['plugins'], ['./kitty-agent-state'])
                before = self.files(root)
                integration.install(agent.id)
                self.assertEqual(self.files(root), before)
                self.assertEqual(integration.read_plugin_config(config_path)['$schema'], 'https://opencode.ai/cli.json')
                directory = os.path.dirname(integration.reporter_path(agent))
                extra = os.path.join(directory, 'user-data')
                with open(extra, 'w') as f:
                    f.write('keep me\n')
                integration.uninstall(agent.id)
                self.assertFalse(os.path.exists(integration.reporter_path(agent)))
                with open(extra) as f:
                    self.assertEqual(f.read(), 'keep me\n')
                with open(unrelated) as f:
                    self.assertEqual(f.read(), 'user plugin\n')
                before = self.files(root)
                integration.uninstall(agent.id)
                self.assertEqual(self.files(root), before)

    def test_agent_integration_plugin_legacy_uninstall(self):
        with self.isolated_agent('opencode', create_config=True) as (root, agent, config_path):
            base = os.path.dirname(config_path)
            legacy = os.path.join(base, 'kitty-agent-state.js')
            with open(legacy, 'w') as f:
                f.write('// ' + integration.KITTY_ASSET_MARKER + '\nlegacy reporter\n')
            old_config = os.path.join(base, 'tui.jsonc')
            with open(old_config, 'w') as f:
                f.write('{"plugin": ["keep", "./kitty-agent-state.js"], "theme": "dark"}')
            integration.uninstall(agent.id)
            self.assertFalse(os.path.lexists(legacy))
            self.assertFalse(os.path.exists(config_path))
            self.assertEqual(integration.read_plugin_config(old_config), {'plugin': ['keep'], 'theme': 'dark'})
            before = self.files(root)
            integration.uninstall(agent.id)
            self.assertEqual(self.files(root), before)

    def test_agent_integration_plugin_migration_preflight(self):
        for conflict in ('legacy', 'legacy-directory', 'target', 'directory', 'directory-symlink', 'target-symlink', 'legacy-symlink', 'config', 'legacy-config', 'legacy-plugins'):
            with self.subTest(conflict=conflict), self.isolated_agent('opencode', create_config=True) as (root, agent, config_path):
                base = os.path.dirname(config_path)
                legacy = os.path.join(base, 'kitty-agent-state.js')
                reporter = integration.reporter_path(agent)
                directory = os.path.dirname(reporter)
                with open(legacy, 'w') as f:
                    f.write('// ' + integration.KITTY_ASSET_MARKER + '\nlegacy reporter\n')
                old_config = os.path.join(base, 'tui.jsonc')
                with open(old_config, 'w') as f:
                    f.write('{"plugin": ["keep", "./kitty-agent-state.js"], "theme": "dark"}')
                if conflict == 'legacy':
                    with open(legacy, 'w') as f:
                        f.write('user legacy reporter\n')
                elif conflict == 'legacy-directory':
                    os.remove(legacy)
                    os.mkdir(legacy)
                elif conflict == 'target':
                    os.mkdir(directory)
                    with open(reporter, 'w') as f:
                        f.write('user reporter\n')
                elif conflict == 'directory':
                    with open(directory, 'w') as f:
                        f.write('user file\n')
                elif conflict in ('directory-symlink', 'target-symlink', 'legacy-symlink'):
                    outside = os.path.join(root, 'outside')
                    os.mkdir(outside)
                    owned = os.path.join(outside, 'tui.js')
                    with open(owned, 'w') as f:
                        f.write('// ' + integration.KITTY_ASSET_MARKER + '\nuser content\n')
                    if conflict == 'directory-symlink':
                        os.symlink(outside, directory)
                    elif conflict == 'target-symlink':
                        os.mkdir(directory)
                        os.symlink(owned, reporter)
                    else:
                        os.remove(legacy)
                        os.symlink(owned, legacy)
                else:
                    path = config_path if conflict == 'config' else old_config
                    with open(path, 'w') as f:
                        f.write('{"plugin": {}}' if conflict == 'legacy-plugins' else '{invalid JSON')
                before = self.files(root)
                state = 'error' if conflict in ('config', 'legacy-config', 'legacy-plugins') else 'conflict'
                self.inspect_state(root, agent, state)
                for operation in (integration.install, integration.uninstall):
                    with self.assertRaises(SystemExit):
                        operation(agent.id)
                    self.assertEqual(self.files(root), before)

    def test_agent_integration_conflict_preflight(self):
        for agent_id in ('claude', 'codex', 'pi', 'omp', 'opencode'):
            with self.subTest(agent=agent_id), self.isolated_agent(agent_id, create_config=True) as (root, agent, config_path):
                missing = self.inspect_state(root, agent, 'missing')
                if agent.integration_kind == 'hook':
                    with open(config_path, 'w') as f:
                        json.dump({'hooks': {'SessionStart': []}}, f)
                elif agent.integration_kind == 'tui-plugin':
                    with open(missing.paths[0], 'w') as f:
                        json.dump({'plugins': ['other-plugin']}, f)
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
            ('opencode', '{"plugins": {}}'),
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
