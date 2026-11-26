#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, kitty contributors

import ast
import os
import re
import sys
from pathlib import Path

from kitty.constants import kitty_base_dir

from .base import BaseTest


class TestUnitTestIsolation(BaseTest):
    def test_default_discovery_excludes_gui_checks(self):
        from .main import find_all_tests, itertests

        for test in itertests(find_all_tests()):
            self.assertFalse(test.__class__.__module__.startswith('gui_tests.'), test.id())

    def test_unit_test_sources_have_no_native_gui_entrypoints(self):
        # Guard the concrete AppKit entry points that previously slipped into
        # default discovery, including compiled Objective-C probe strings.
        # This is not a sandbox: new launch helpers still require code review.
        root = Path(os.getcwd() if getattr(sys, 'frozen', False) else kitty_base_dir)
        forbidden = {'NSWindow', 'sharedApplication', 'standardUserDefaults'}
        native = re.compile(r'\[\s*(?:NSApplication\s+sharedApplication|NSUserDefaults\s+standardUserDefaults|NSWindow\s+alloc)\b')
        violations = []
        for path in sorted((root / 'kitty_tests').glob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    for arg in node.args:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value in forbidden:
                            violations.append(f'{path.name}:{node.lineno}: {arg.value}')
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and native.search(node.value):
                    violations.append(f'{path.name}:{node.lineno}: native AppKit application probe')
        self.ae(violations, [], 'Move real GUI checks to gui_tests; do not run them as unit tests')
