#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

import os
from collections.abc import Mapping, Sequence
from typing import Any

from .agents import AgentSidebarRow, AgentState

EDITOR_EXECUTABLES = {
    'nvim': 'nvim', 'nvimdiff': 'nvim',
    'vim': 'vim', 'vimdiff': 'vim', 'vi': 'vi', 'view': 'view',
    'nano': 'nano', 'micro': 'micro',
    'hx': 'helix', 'helix': 'helix',
    'kak': 'kakoune',
    'emacs': 'emacs', 'emacsclient': 'emacs',
}
VIM_OPTIONS_WITH_ARGUMENT = frozenset(('-c', '--cmd', '-u', '-U', '-S', '-i', '-t', '--server', '--listen'))
VIM_NONINTERACTIVE_FLAGS = frozenset((
    '--headless', '--embed', '-es', '-Es', '-se', '-sE', '-e', '-E', '-s', '-g', '--help', '--version'))
OPTIONS_WITH_ARGUMENT = {
    'nvim': VIM_OPTIONS_WITH_ARGUMENT, 'vim': VIM_OPTIONS_WITH_ARGUMENT,
    'vi': VIM_OPTIONS_WITH_ARGUMENT, 'view': VIM_OPTIONS_WITH_ARGUMENT,
    'nano': frozenset(), 'micro': frozenset(),
    'hx': frozenset(), 'helix': frozenset(),
    'kak': frozenset(('-c', '-C', '-e', '-E', '-s', '-p', '-f', '-i', '-ui', '--ui', '--debug', '-debug')),
    'emacs': frozenset((
        '-u', '--user', '-L', '--directory', '-l', '--load', '--eval', '--visit', '--find-file', '-F', '--funcall')),
    'emacsclient': frozenset((
        '-a', '--alternate-editor', '-s', '--socket-name', '-f', '--server-file', '-e', '--eval')),
}
NONINTERACTIVE_FLAGS = {
    'nano': frozenset(('-h', '-?', '--help', '-V', '--version')),
    'micro': frozenset(('-help', '--help', '-version', '--version')),
    'hx': frozenset(('-h', '--help', '-V', '--version', '--health', '-g', '--grammar')),
    'helix': frozenset(('-h', '--help', '-V', '--version', '--health', '-g', '--grammar')),
    'kak': frozenset((
        '-d', '--daemon', '-p', '--pipe', '-f', '--filter', '-l', '--list', '--clear', '-clear',
        '-help', '--help', '-version', '--version')),
    'emacs': frozenset(('--help', '--version', '--batch', '--daemon', '--script')),
    'emacsclient': frozenset(('--help', '--version', '-e', '--eval', '--batch')),
}
TUI_FLAGS = {
    'emacs': frozenset(('-nw', '--no-window-system')),
    'emacsclient': frozenset(('-nw', '--no-window-system', '-t', '--tty')),
}
VIM_EDITORS = frozenset(('nvim', 'vim', 'vi', 'view'))


def detect_editor(processes: Sequence[Mapping[str, Any]]) -> str | None:
    """Recognize terminal editors using a bounded set of common argv options.

    Wrappers are not followed. GUI, batch, daemon, filter and headless modes
    are deliberately excluded from this interactive-only detector.
    """
    for process in processes:
        argv = process.get('cmdline') or ()
        if not argv:
            continue
        name = os.path.basename(argv[0])
        editor = EDITOR_EXECUTABLES.get(name)
        if editor is None:
            continue
        interactive = True
        has_tui = False
        args = iter(argv[1:])
        for arg in args:
            if arg == '--':
                break
            if name == 'kak' and (arg in ('-ui', '--ui') or arg.startswith(('-ui=', '--ui='))):
                ui = arg.partition('=')[2] if '=' in arg else next(args, '')
                if ui != 'terminal':
                    interactive = False
                    break
                continue
            if name in VIM_EDITORS:
                if (arg in VIM_NONINTERACTIVE_FLAGS or (name == 'nvim' and arg == '-v')
                        or (arg.startswith('--remote') and arg != '--remote-ui')):
                    interactive = False
                    break
            elif arg in NONINTERACTIVE_FLAGS.get(name, ()):
                interactive = False
                break
            if arg in TUI_FLAGS.get(name, ()):
                has_tui = True
            options = OPTIONS_WITH_ARGUMENT.get(name)
            if options and arg in options:
                # Consuming a missing argument is harmless here; this is
                # detection, not argv validation.
                next(args, None)
        if name in TUI_FLAGS and not has_tui:
            interactive = False
        if interactive:
            return editor
    return None


def editor_row(window: Any, is_active: bool) -> AgentSidebarRow | None:
    """Use kitty's foreground cache only; stopped/background jobs disappear."""
    editor = detect_editor(window.child.foreground_processes)
    if editor is None:
        return None
    cwd = window.cwd_of_child or ''
    title = f'{editor} · {window.title}' if window.title else editor
    return AgentSidebarRow(window.id, editor, f'editor:{editor}', editor, title, os.path.basename(cwd) or cwd, AgentState.unknown, is_active, 'editor')
