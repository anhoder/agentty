#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, kitty contributors

import ast
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kitty.constants import kitty_base_dir

from .base import BaseTest


def source_root() -> Path:
    # bypy runs frozen tests with the writable source checkout as cwd.
    return Path(os.getcwd() if getattr(sys, 'frozen', False) else kitty_base_dir)


def helpers(filename, names, namespace):
    # Packaging entry points require bypy's injected globals and bootstrap.
    # Load only the functions under test, never execute that bootstrap.
    path = source_root() / 'bypy' / filename
    tree = ast.parse(path.read_text())
    tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    exec(compile(tree, str(path), 'exec'), namespace)
    return namespace


class MacOSDebugSymbols(BaseTest):

    def test_frozen_source_root(self):
        from . import macos_debug_symbols as module

        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            source = base / 'src'
            for filename, names in (
                ('init_env.py', {'setup_py_options', 'build_c_extensions', 'build_frozen_launcher', 'build_frozen_tools'}),
                ('macos/__main__.py', {'stage_dsym', 'dwarf_uuids', 'archive_dsyms', 'files_in', 'Freeze'}),
            ):
                original = source_root() / 'bypy' / filename
                tree = ast.parse(original.read_text())
                tree.body = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
                dest = source / 'bypy' / filename
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(ast.unparse(tree))
            frozen = base / 'python-lib.bypy.frozen'
            frozen.touch()
            with patch.object(module, '__file__', str(frozen / 'kitty_tests/macos_debug_symbols.py')), \
                    patch.object(sys, 'frozen', True, create=True), patch.object(os, 'getcwd', return_value=str(source)):
                self.test_debug_setup_flags()
                self.test_dsym_preservation_and_validation()
                self.test_symbol_staging_lifecycle()

    def test_debug_setup_flags(self):
        import contextlib

        for macos, debug in ((True, True), (True, False), (False, True)):
            with tempfile.TemporaryDirectory() as root:
                src = Path(root) / 'original'
                src.mkdir()
                ext = Path(root) / 'ext'
                commands = []
                ns = helpers('init_env.py', {'setup_py_options', 'build_c_extensions', 'build_frozen_launcher', 'build_frozen_tools'}, {
                    'os': os, 'shutil': shutil, 'suppress': contextlib.suppress,
                    'KITTY_DIR': str(src), 'ismacos': macos, 'SETUP_CMD': ['python', 'setup.py'],
                    'kitty_constants': {'appname': 'kitty'}, 'run': lambda *args, **kw: commands.append(args) or 0,
                })
                ns['build_c_extensions'](str(ext), SimpleNamespace(dont_strip=debug, extra_program_data='revision'))
                ns['build_frozen_launcher']([])
                ns['build_frozen_tools']('/tmp/kitty')
                for cmd in commands:
                    self.ae('--debug' in cmd, debug)
                    self.ae('--build-dSYM' in cmd, macos and debug)
                    self.assertIn('--vcs-rev=revision', cmd)

    def test_dsym_preservation_and_validation(self):
        import glob
        import re

        ns = helpers('macos/__main__.py', {'stage_dsym', 'dwarf_uuids', 'archive_dsyms', 'files_in'}, {
            'os': os, 'shutil': shutil, 'subprocess': subprocess, 'glob': glob, 're': re,
            'json': json, 'tarfile': tarfile, 'join': os.path.join, 'basename': os.path.basename,
        })
        uuid = '12345678-1234-1234-1234-123456789ABC'
        output = ''.join(f'UUID: {uuid} ({arch}) file with spaces\n' for arch in ('arm64', 'x86_64'))
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            binary = base / 'kitty'
            source = base / 'kitty.dSYM'
            source.mkdir()
            (source / 'data').write_text('preserved')
            stage = base / 'staging'
            stage.mkdir()
            def dsymutil(cmd):
                self.ae(cmd, ['xcrun', 'dsymutil', str(binary), '-o', str(stage / source.name)])
                shutil.copytree(source, cmd[-1])

            with patch.object(subprocess, 'check_call', side_effect=dsymutil), \
                    patch.object(subprocess, 'check_output', return_value=output):
                ns['stage_dsym'](str(binary), str(stage / source.name))
            (stage / 'extension-map.json').write_text('{}')
            shutil.rmtree(source)
            app = base / 'app'
            app.mkdir()
            launcher = app / 'Contents/MacOS/kitty'
            launcher.parent.mkdir(parents=True)
            launcher.write_bytes(b'\xcf\xfa\xed\xfe' + bytes(28))
            quick_access = app / 'Contents/kitty-quick-access.app/Contents/MacOS/kitty-quick-access'
            quick_access.parent.mkdir(parents=True)
            quick_access.write_bytes(launcher.read_bytes())
            script = app / 'Contents/Resources/kitty/shell-integration/ssh/kitty'
            script.parent.mkdir(parents=True)
            script.write_text('#!/bin/sh\nexec kitty "$@"\n')
            script.chmod(0o755)
            archive = base / 'symbols.tar.xz'
            def dwarf_output(cmd, **kwargs):
                self.assertNotEqual(cmd[-1], str(script), 'SSH integration script is not a Mach-O binary')
                return 'DW_TAG_compile_unit DW_TAG_structure_type' if '--debug-info' in cmd else output

            tar_open = tarfile.open

            def open_archive(name, mode='r', **kwargs):
                # Compression is a host-packaging capability checked separately;
                # frozen test runtimes need not include lzma. Still test real tar contents.
                if name == str(archive) and mode == 'w:xz':
                    mode = 'w'
                return tar_open(name, mode, **kwargs)

            with patch.object(subprocess, 'check_output', side_effect=dwarf_output), patch.object(subprocess, 'check_call'), \
                    patch.object(tarfile, 'open', side_effect=open_archive):
                ns['archive_dsyms'](str(stage), str(app), str(archive))
            with tarfile.open(archive) as tf:
                self.assertIn('kitty.dSYM/data', tf.getnames())
                manifest = json.load(tf.extractfile('manifest.json'))
                self.ae(set(manifest['kitty.dSYM']['uuids']), {'arm64', 'x86_64'})
                self.ae(set(manifest['kitty.dSYM']['binaries']), {
                    str(launcher.relative_to(app)), str(quick_access.relative_to(app)),
                })
            with patch.object(subprocess, 'check_output', return_value=output), patch.object(subprocess, 'check_call'):
                with self.assertRaisesRegex(RuntimeError, 'full DWARF'):
                    ns['archive_dsyms'](str(stage), str(app), str(archive))
            (app / 'kitty.fast_data_types.so').touch()
            with patch.object(subprocess, 'check_output', side_effect=lambda cmd, **kw: (
                output.replace(uuid, 'AAAAAAAA-1234-1234-1234-123456789ABC') if 'fast_data_types' in cmd[-1] else dwarf_output(cmd, **kw)
            )), patch.object(subprocess, 'check_call'):
                with self.assertRaisesRegex(RuntimeError, 'Missing requested'):
                    ns['archive_dsyms'](str(stage), str(app), str(archive))
            (app / 'kitty.fast_data_types.so').unlink()
            with self.assertRaisesRegex(RuntimeError, 'Missing requested'):
                ns['stage_dsym'](str(binary), str(stage / 'missing'))
            with patch.object(subprocess, 'check_output', side_effect=[output, output, output.splitlines()[0]]):
                with self.assertRaisesRegex(RuntimeError, 'mismatch'):
                    ns['archive_dsyms'](str(stage), str(app), str(archive))
            for invalid in ('', 'garbage', output + output):
                with patch.object(subprocess, 'check_output', return_value=invalid):
                    with self.assertRaises(RuntimeError):
                        ns['dwarf_uuids']('binary')
            with patch.object(subprocess, 'check_output', return_value=''):
                with self.assertRaisesRegex(RuntimeError, 'No debug UUIDs'):
                    ns['archive_dsyms'](str(stage), str(app), str(archive))

    def test_universal_dsym_regeneration(self):
        import re

        ns = helpers('macos/__main__.py', {'stage_dsym', 'dwarf_uuids'}, {
            'os': os, 'subprocess': subprocess, 're': re,
        })
        thin = 'UUID: A87C5A1D-5F03-3832-9956-B25B09847989 (x86_64) rsync.so\n'
        universal = thin + 'UUID: 4F15F44C-BA4D-3CA4-811A-4EFF2D5BADEF (arm64) rsync.so\n'
        with tempfile.TemporaryDirectory() as root:
            binary = str(Path(root) / 'rsync.so')
            Path(binary + '.dSYM').mkdir()
            dest = str(Path(root) / 'staged.dSYM')
            with patch.object(subprocess, 'check_call') as regenerate, \
                    patch.object(subprocess, 'check_output', side_effect=[universal, universal]):
                ns['stage_dsym'](binary, dest)
                regenerate.assert_called_once_with(['xcrun', 'dsymutil', binary, '-o', dest])
            with patch.object(subprocess, 'check_call'), \
                    patch.object(subprocess, 'check_output', side_effect=[universal, thin]):
                with self.assertRaisesRegex(RuntimeError, 'UUID/architecture mismatch'):
                    ns['stage_dsym'](binary, dest)

    def test_symbol_staging_lifecycle(self):
        path = source_root() / 'bypy/macos/__main__.py'
        tree = ast.parse(path.read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Freeze')
        methods = {node.name: ast.unparse(node) for node in cls.body if isinstance(node, ast.FunctionDef)}
        self.assertLess(methods['__init__'].index('stage_dsym('), methods['__init__'].index('self.run()'))
        self.assertLess(methods['freeze_python'].index('stage_dsym('), methods['freeze_python'].index("shutil.rmtree(join(dirname(self.contents_dir), 'bin'))"))
        self.assertLess(methods['makedmg'].index('shutil.rmtree(destdir)'), methods['makedmg'].index('archive_dsyms('))
        self.assertLess(methods['makedmg'].index("'--verify'"), methods['makedmg'].index('archive_dsyms(self.symbol_dir, appdir,'))
