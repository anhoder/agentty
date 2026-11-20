#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

import os
from collections.abc import Mapping, Sequence
from typing import Any

from .agents import AgentSidebarRow, AgentState

EDITOR_EXECUTABLES = {'nvim': 'nvim', 'vim': 'vim', 'vi': 'vi', 'view': 'view', 'nvimdiff': 'nvim', 'vimdiff': 'vim'}
OPTIONS_WITH_ARGUMENT = frozenset(('-c', '--cmd', '-u', '-U', '-S', '-i', '-t', '--server', '--listen'))


def detect_editor(processes: Sequence[Mapping[str, Any]]) -> str | None:
    """Recognize terminal editors using a bounded set of common argv options.

    Wrappers are not followed. Ex/batch modes and Vim -s script input are
    deliberately excluded from this interactive-only MVP.
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
        args = iter(argv[1:])
        for arg in args:
            if arg == '--':
                break
            if arg in OPTIONS_WITH_ARGUMENT:
                # -S alone sources Session.vim. Consuming a missing argument
                # is harmless here; this is detection, not argv validation.
                next(args, None)
                continue
            if (arg in ('--headless', '--embed', '-es', '-Es', '-se', '-sE', '-e', '-E', '-s', '-g', '--help', '--version')
                    or (editor == 'nvim' and arg == '-v')
                    or (arg.startswith('--remote') and arg != '--remote-ui')):
                interactive = False
                break
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
