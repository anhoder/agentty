#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, kitty contributors

from types import SimpleNamespace
from unittest.mock import patch

from .base import BaseTest


class TestSidebarFonts(BaseTest):
    def test_sidecar_grouping_configuration(self):
        from kitty.config import load_config
        from kitty.options.types import Options, defaults

        self.ae(defaults.sidecar_grouping, 'time')
        for grouping in ('time', 'agent', 'tab', 'path'):
            with self.subTest(grouping=grouping):
                errors = []
                opts = load_config(overrides=(f'sidecar_grouping {grouping}',), accumulate_bad_lines=errors)
                self.ae(errors, [])
                self.ae(opts.sidecar_grouping, grouping)
                self.ae(Options(opts._asdict()).sidecar_grouping, grouping)

    def test_sidebar_font_configuration(self):
        from kitty.config import load_config
        from kitty.options.types import Options, defaults

        self.ae(defaults.sidecar_font_family, 'system')
        for name in ('system', 'Menlo', 'Symbols Nerd Font Mono', 'Missing-Sidebar-Font'):
            with self.subTest(name=name):
                errors = []
                opts = load_config(overrides=(f'sidecar_font_family {name}',), accumulate_bad_lines=errors)
                self.ae(errors, [])
                self.ae(opts.sidecar_font_family, name)
                self.ae(opts.font_family, defaults.font_family)
                self.ae(Options(opts._asdict()).sidecar_font_family, name)

    def test_sidebar_font_update_bridge(self):
        from kitty.tabs import TabManager

        manager = SimpleNamespace(os_window_id=1, agent_sidebar_enabled=True, agent_sidebar_cols=32, agent_sidebar_rows=())
        with (
            patch('kitty.tabs.is_macos', True),
            patch('kitty.tabs.get_boss', return_value=SimpleNamespace(agent_sidebar_collapsed_groups=())),
            patch('kitty.tabs.set_agents_sidebar') as update,
        ):
            for name in ('Menlo', 'system'):
                self.set_options({'sidecar_font_family': name})
                TabManager.update_native_agents_sidebar(manager)
                self.ae(update.call_args.args[-1], name)
