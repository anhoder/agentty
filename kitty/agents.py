#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

import os
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from enum import Enum
from functools import lru_cache
from typing import Any

from .agent_manifest import ManifestState, Screen, StateAuthority, authority_for, detect, is_valid_session_id, manifest_for
from .fast_data_types import monotonic


class AgentState(str, Enum):
    idle = 'idle'
    working = 'working'
    blocked = 'blocked'
    done = 'done'
    unknown = 'unknown'


# Manifest rules never assert done: that is a lifecycle outcome reported over
# OSC 7501, not something visible on screen.
STATE_MAP = {
    ManifestState.idle: AgentState.idle,
    ManifestState.working: AgentState.working,
    ManifestState.blocked: AgentState.blocked,
    ManifestState.unknown: AgentState.unknown,
}


AGENT_SOURCE_VAR = 'KITTY_AGENT_SOURCE'
AGENT_ID_VAR = 'KITTY_AGENT_ID'
AGENT_SESSION_VAR = 'KITTY_AGENT_SESSION'
AGENT_SESSION_PATH_VAR = 'KITTY_AGENT_SESSION_PATH'


@dataclass(frozen=True)
class AgentSnapshot:
    window_id: int
    agent: str
    state: AgentState
    title: str
    cwd: str
    # The agent's own conversation id, empty unless the agent reported one
    session_id: str = ''
    generation: int = 0


@dataclass(frozen=True)
class AgentSidebarRow:
    window_id: int
    group: str
    # Identity the UI keys collapse/hover/animation state on. The display label
    # above is derived from mutable things (tab titles change while an agent
    # works), so it cannot double as the identity: a renamed group would lose
    # its collapsed state and spring back open.
    group_key: str
    agent: str
    title: str
    detail: str
    state: AgentState
    is_active: bool
    kind: str = 'agent'
    project_key: str = ''


@lru_cache(maxsize=256)
def project_key_for_cwd(cwd: str) -> str:
    """Return a stable key for linking sidebar rows in the same project.

    A Git worktree top-level groups edits in subdirectories; otherwise the
    normalized working directory is the project boundary.
    """
    if not cwd:
        return ''
    path = os.path.realpath(cwd)
    current = path
    while True:
        if os.path.exists(os.path.join(current, '.git')):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return os.path.normpath(path)


# Maps a foreground executable name to a manifest id. The value side must match
# the manifest `id` field, because that is the key the rule tables are looked
# up under. An unrecognized foreground process is still a normal terminal, not
# an unknown agent row.
AGENT_EXECUTABLES: dict[str, str] = {
    'amp': 'amp',
    'agy': 'agy',
    'antigravity': 'agy',
    'claude': 'claude',
    'cline': 'cline',
    'codex': 'codex',
    'copilot': 'copilot',
    'cursor-agent': 'cursor',
    'devin': 'devin',
    'droid': 'droid',
    'gemini': 'gemini',
    'grok': 'grok',
    'hermes': 'hermes',
    'kimi': 'kimi',
    'kiro': 'kiro',
    'kiro-cli': 'kiro',
    'kilo': 'kilo',
    'letta': 'letta',
    'maki': 'maki',
    'mastracode': 'mastracode',
    'muse': 'muse',
    'omp': 'omp',
    'opencode': 'opencode',
    'pi': 'pi',
    'qoder': 'qodercli',
    'qodercli': 'qodercli',
    'qwen': 'qwen',
}


def _executable_name(value: str) -> str:
    name = os.path.basename(value).lower()
    if name.endswith(('.cmd', '.exe')):
        name = name.rsplit('.', 1)[0]
    return name


def _candidate_names(process: Mapping[str, Any]) -> Sequence[str]:
    cmdline = process.get('cmdline')
    if not cmdline:
        return ()
    names = [_executable_name(str(cmdline[0]))]
    if names[0] in {'env', 'node', 'bun', 'deno', 'python', 'python3', 'ruby', 'java'} and len(cmdline) > 1:
        names.append(_executable_name(str(cmdline[1])))
        script = str(cmdline[1]).replace('\\', '/')
        if any(script.endswith(f'/{scope}/pi-coding-agent/dist/cli.js') for scope in ('@earendil-works', '@mariozechner')):
            names.append('pi')
    return tuple(names)


def detect_agent(processes: Sequence[Mapping[str, Any]]) -> str | None:
    """Return the first recognized foreground agent, if any."""
    for process in processes:
        for candidate in _candidate_names(process):
            if candidate in AGENT_EXECUTABLES:
                return AGENT_EXECUTABLES[candidate]
    return None


def state_from_signals(
    agent: str | None,
    program_status: Sequence[Mapping[str, Any]],
    screen: Screen | None = None,
    previous: AgentState | None = None,
    session_id: str = '',
) -> AgentState:
    """Arbitrate a semantic report against visible screen evidence."""
    return arbitrate(agent, program_status, screen, previous, session_id)[0]


def arbitrate(
    agent: str | None,
    program_status: Sequence[Mapping[str, Any]],
    screen: Screen | None = None,
    previous: AgentState | None = None,
    session_id: str = '',
) -> tuple[AgentState, bool]:
    """Return the state plus whether a visible idle marker backs it.

    A lifecycle report is scoped to the window's current root session. Without
    that identity, only OpenCode's app-tagged native root summary is usable;
    other reports cannot be attributed safely and screen rules are the fallback.
    """
    if agent is None:
        return AgentState.unknown, False
    reported = _reported_state(program_status, session_id, agent)
    if reported is not None and authority_for(agent) is StateAuthority.full_lifecycle:
        return reported, reported is AgentState.idle
    match = detect(agent, screen) if screen is not None else None
    if match is None or match.manifest is None:
        return reported or AgentState.unknown, False
    if not match.manifest.rules:
        return reported or AgentState.unknown, False
    if match.skipped:
        held = previous if previous is not None else (reported or AgentState.unknown)
        return held, False
    if match.rule is None:
        return reported or AgentState.idle, False
    return STATE_MAP[match.state], match.visible_idle


def _is_session_record(record_id: Any, session_id: str) -> bool:
    return record_id == session_id or (isinstance(record_id, str) and record_id.startswith(session_id + '/'))


def _reported_state(program_status: Sequence[Mapping[str, Any]], session_id: str = '', agent: str | None = None) -> AgentState | None:
    if session_id:
        program_status = tuple(record for record in program_status if _is_session_record(record.get('id', ''), session_id))
    elif agent == 'opencode':
        # OpenCode v2 emits a native window summary without a session identity.
        # Only that app-tagged root is attributable; named records can belong
        # to other sessions in its shared tab strip.
        program_status = tuple(record for record in program_status if record.get('id', '') == '' and record.get('app') == 'opencode')
    states = {str(record.get('state', '')) for record in program_status}
    if 'blocked' in states:
        return AgentState.blocked
    if 'error' in states or 'done' in states:
        return AgentState.done
    if 'working' in states:
        return AgentState.working
    if 'idle' in states:
        return AgentState.idle
    return None


def status_scope(program_status: Sequence[Mapping[str, Any]], session_id: str = '') -> dict[str, Any]:
    records = tuple(program_status)
    if not session_id:
        return {'root_session': '', 'matched_ids': [], 'ignored_ids': [str(r.get('id', '')) for r in records if r.get('id', '')]}
    matched = [str(r.get('id', '')) for r in records if _is_session_record(r.get('id', ''), session_id)]
    ignored = [str(r.get('id', '')) for r in records if r.get('id', '') and not _is_session_record(r.get('id', ''), session_id)]
    return {'root_session': session_id, 'matched_ids': matched, 'ignored_ids': ignored}


def session_ref_of_window(window: Any, agent: Any) -> str:
    """Return the Herdr-compatible id or absolute session path reported by an agent."""
    from .agent_manifest import is_valid_session_path

    user_vars = getattr(window, 'user_vars', None) or {}
    reported_agent = user_vars.get(AGENT_ID_VAR, '')
    if reported_agent:
        manifest = manifest_for(reported_agent)
        if manifest is None or manifest.id != agent:
            return ''
    reported_source = user_vars.get(AGENT_SOURCE_VAR, '')
    if reported_source and reported_source not in {f'kitty:{agent}', f'herdr:{agent}'}:
        return ''
    session_path = user_vars.get('KITTY_AGENT_SESSION_PATH', '')
    if session_path and agent in {'pi', 'omp'} and is_valid_session_path(session_path):
        return session_path
    session_id = user_vars.get(AGENT_SESSION_VAR, '')
    if session_id and is_valid_session_id(session_id):
        return session_id
    return ''


def session_id_of_window(window: Any, agent: str) -> str:
    return session_ref_of_window(window, agent)


def resume_argv_for(agent: str, session_ref: str) -> tuple[str, ...]:
    """Build the manifest command from a Herdr-compatible id or path reference."""
    from .agent_manifest import is_valid_session_path

    if not session_ref:
        return ()
    if agent in {'pi', 'omp'}:
        if not (is_valid_session_id(session_ref) or is_valid_session_path(session_ref)):
            return ()
    elif not is_valid_session_id(session_ref):
        return ()
    manifest = manifest_for(agent)
    if manifest is None or manifest.resume is None:
        return ()
    return manifest.resume.command_for(session_ref)


def known_agent_resume_command(cmd: Sequence[str], shell: Sequence[str]) -> str:
    """Recognize a canonical manifest resume command, independent of current vars.

    Reconstruct rather than interpret shell text, including shell-specific quoting.
    Extra commands, redirections and user-authored flags are not resume wrappers.
    """
    import shlex

    from .agent_manifest import SESSION_PLACEHOLDER
    from .shell_integration import get_supported_shell_name, join

    if not shell or len(cmd) != len(shell) + 2 or list(cmd[:-2]) != list(shell) or cmd[-2] != '-ilc':
        return ''
    if not cmd[-1].startswith('exec '):
        return ''
    shell_name = get_supported_shell_name(shell[0])
    lexer = shlex.shlex(cmd[-1][5:], posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ''
    if shell_name == 'fish':
        lexer.escapedquotes = "'"
    try:
        argv = tuple(lexer)
    except ValueError:
        return ''
    agent = detect_agent([{'cmdline': argv}])
    if not agent:
        return ''
    manifest = manifest_for(agent)
    if manifest is None or manifest.resume is None or len(argv) != len(manifest.resume.argv):
        return ''
    for template, value in zip(manifest.resume.argv, argv):
        if SESSION_PLACEHOLDER in template:
            prefix, _, suffix = template.partition(SESSION_PLACEHOLDER)
            if not value.startswith(prefix) or not value.endswith(suffix):
                return ''
            reference = value[len(prefix):len(value) - len(suffix) if suffix else None]
            if resume_argv_for(agent, reference) != argv:
                return ''
            command = join(shell[0], argv) if shell_name else shlex.join(argv)
            return agent if cmd[-1] == 'exec ' + command else ''
    return ''


def snapshot_for_window(
    window: Any,
    screen: Screen | None = None,
    previous: AgentState | None = None,
    processes: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[AgentSnapshot | None, bool]:
    processes = window.child.foreground_processes if processes is None else processes
    agent = detect_agent(processes)
    if agent is None:
        return None, False
    session_id = session_id_of_window(window, agent)
    state, visible_idle = arbitrate(agent, window.program_status.as_list(), screen, previous, session_id)
    snapshot = AgentSnapshot(
        window_id=window.id,
        agent=agent,
        state=state,
        title=window.title,
        cwd=window.cwd_of_child or window.child.cwd,
        session_id=session_id,
    )
    return snapshot, visible_idle


class AgentTracker:
    """Cache per-window agent snapshots and coalesce event-driven refreshes.

    Also holds back one specific transition: working to a plain idle that has
    no visible idle marker behind it. Agent TUIs repaint their status area in
    several steps, so for a frame or two mid-turn the working marker is gone
    while the prompt box has not been drawn yet. Publishing idle there makes
    the sidebar flicker. A transition backed by visible evidence, and every
    other transition, applies immediately.
    """

    IDLE_DEBOUNCE = 0.25

    def __init__(self) -> None:
        self.snapshots: dict[int, AgentSnapshot | None] = {}
        self.dirty_windows: set[int] = set()
        self.pending_idle_since: dict[int, float] = {}
        # Windows whose turn finished while the user was looking elsewhere.
        self.unseen_done: set[int] = set()
        self.event_seq = 0
        self.events: deque[dict[str, Any]] = deque(maxlen=256)
        self.identities: dict[int, tuple[Any, ...]] = {}
        self.generations: dict[int, int] = {}
        self.detection_cache: dict[int, tuple[Any, Screen]] = {}

    def observe(self, window: Any) -> AgentSnapshot | None:
        from .child import cached_process_data

        with cached_process_data():
            before = self.snapshot_for(window.id)
            self.refresh_window(window)
            after = self.snapshot_for(window.id)
            self.record_change(window.id, before, after)
            return after

    def record_change(self, window_id: int, before: AgentSnapshot | None, after: AgentSnapshot | None) -> None:
        """Called only by the Boss flush, after tracker arbitration/debounce."""
        if before != after:
            self.event_seq += 1
            self.events.append(
                {
                    'seq': self.event_seq,
                    'window_id': window_id,
                    'generation': self.generations.get(window_id, 0),
                    'identity': self.identities.get(window_id),
                    'reason': 'changed' if after is not None else 'agent_removed',
                    'snapshot': asdict(after) if after is not None else None,
                }
            )

    def event_batch(self, window_id: int, after_seq: int, maximum: int) -> dict[str, Any]:
        snapshot = self.snapshots.get(window_id)
        if self.events and after_seq < self.events[0]['seq'] - 1:
            return {
                'reason': 'truncated',
                'resync_required': True,
                'next_seq': self.event_seq,
                'head_seq': self.event_seq,
                'has_more': False,
                'resync_cursor': self.event_seq,
                'snapshot': asdict(snapshot) if snapshot is not None else None,
                'events': [],
            }
        events = [e for e in self.events if e['window_id'] == window_id and e['seq'] > after_seq][:maximum]
        next_seq = events[-1]['seq'] if events else self.event_seq
        more = any(e['window_id'] == window_id and e['seq'] > next_seq for e in self.events)
        return {'reason': 'changed' if events else 'timeout', 'next_seq': next_seq, 'head_seq': self.event_seq, 'has_more': more, 'events': events}

    def mark_dirty(self, window_id: int) -> None:
        self.dirty_windows.add(window_id)

    def mark_all_dirty(self, window_ids: Sequence[int]) -> None:
        self.dirty_windows.update(window_ids)

    def live_snapshot(self, window: Any, previous: AgentState | None = None, processes: Sequence[Mapping[str, Any]] | None = None) -> tuple[AgentSnapshot | None, bool]:
        """Read current evidence without presentation debounce or completion holds."""
        from .child import cached_process_data

        with cached_process_data():
            processes = window.child.foreground_processes if processes is None else processes
            agent = detect_agent(processes)
            screen = None
            manifest = manifest_for(agent) if agent is not None else None
            session_id = session_id_of_window(window, agent) if agent is not None else ''
            reported = _reported_state(window.program_status.as_list(), session_id, agent)
            if manifest is not None and manifest.rules and not (reported is not None and authority_for(agent) is StateAuthority.full_lifecycle):
                screen_obj = getattr(window, 'screen', None)
                generation = getattr(screen_obj, 'search_generation', None)
                key = (agent, session_id, screen_obj, generation, getattr(screen_obj, 'columns', None),
                       getattr(screen_obj, 'lines', None), window.title, getattr(window, 'progress_payload', None))
                cached = self.detection_cache.get(window.id)
                if generation is not None and cached is not None and cached[0] == key:
                    screen = cached[1]
                else:
                    screen = window.agent_detection_snapshot()
                    if generation is not None:
                        self.detection_cache[window.id] = (key, screen)
            else:
                self.detection_cache.pop(window.id, None)
            return snapshot_for_window(window, screen, previous, processes)

    def drain_dirty(self) -> tuple[int, ...]:
        dirty = tuple(self.dirty_windows)
        self.dirty_windows.clear()
        return dirty

    def refresh_window(self, window: Any, now: float | None = None) -> bool:
        from .child import cached_process_data

        with cached_process_data():
            return self._refresh_window(window, now)

    def _refresh_window(self, window: Any, now: float | None = None) -> bool:
        processes = window.child.foreground_processes
        identity = agent_identity(window, processes)
        if self.identities.get(window.id) != identity:
            self.identities[window.id] = identity
            self.generations[window.id] = self.generations.get(window.id, 0) + 1
            self.snapshots.pop(window.id, None)
            self.detection_cache.pop(window.id, None)
            self.pending_idle_since.pop(window.id, None)
            self.unseen_done.discard(window.id)
        previous = self.snapshots.get(window.id)
        previous_state = previous.state if previous is not None else None
        current, visible_idle = self.live_snapshot(window, previous_state, processes)
        if current is not None:
            current = replace(current, generation=self.generations[window.id])
        if current is not None and previous is not None:
            current = self._debounce_idle(current, previous, visible_idle, now if now is not None else monotonic())
            current = self._hold_done(current, previous, window)
        else:
            self.pending_idle_since.pop(window.id, None)
            self.unseen_done.discard(window.id)
        self.snapshots[window.id] = current
        return previous != current

    def _debounce_idle(self, current: AgentSnapshot, previous: AgentSnapshot, visible_idle: bool, now: float) -> AgentSnapshot:
        going_quiet = previous.state is AgentState.working and current.state is AgentState.idle
        if not going_quiet or visible_idle:
            self.pending_idle_since.pop(current.window_id, None)
            return current
        since = self.pending_idle_since.setdefault(current.window_id, now)
        if now - since >= self.IDLE_DEBOUNCE:
            self.pending_idle_since.pop(current.window_id, None)
            return current
        return previous

    def next_refresh_delay(self, now: float | None = None) -> float | None:
        if not self.pending_idle_since:
            return None
        current = monotonic() if now is None else now
        return max(0.0, min(self.IDLE_DEBOUNCE - (current - since) for since in self.pending_idle_since.values()))

    def pending_idle_windows(self) -> tuple[int, ...]:
        return tuple(self.pending_idle_since)

    def _hold_done(self, current: AgentSnapshot, previous: AgentSnapshot, window: Any) -> AgentSnapshot:
        """Turn a finished turn into done, and keep it until the user looks.

        A screen-detected agent has no way to report completion, so the
        transition out of working is the only evidence a turn ended. Showing
        idle there loses the one piece of information the user wants after
        stepping away: this agent finished while you were not watching.
        """
        if window.is_focused:
            # Looking at the window is seeing it.
            self.unseen_done.discard(current.window_id)
            return current
        if previous.state is AgentState.working and current.state is AgentState.idle:
            self.unseen_done.add(current.window_id)
        if current.window_id in self.unseen_done and current.state is AgentState.idle:
            return replace(current, state=AgentState.done)
        if current.state not in (AgentState.idle, AgentState.done):
            # Started working again, or is now blocked: the completion is no
            # longer the latest thing that happened.
            self.unseen_done.discard(current.window_id)
        return current

    def discard(self, window_id: int) -> None:
        if window_id in self.snapshots or window_id in self.identities:
            self.event_seq += 1
            self.events.append(
                {
                    'seq': self.event_seq,
                    'window_id': window_id,
                    'snapshot': None,
                    'generation': self.generations.get(window_id, 0),
                    'identity': self.identities.get(window_id),
                    'reason': 'window_closed',
                }
            )
        self.identities.pop(window_id, None)
        self.generations.pop(window_id, None)
        self.detection_cache.pop(window_id, None)
        self.dirty_windows.discard(window_id)
        self.snapshots.pop(window_id, None)
        self.pending_idle_since.pop(window_id, None)
        self.unseen_done.discard(window_id)

    def mark_unseen_done(self, window_id: int) -> bool:
        """Keep an idle agent marked done until the user opens it again."""
        snapshot = self.snapshots.get(window_id)
        if snapshot is None or snapshot.state is not AgentState.idle:
            return False
        self.unseen_done.add(window_id)
        self.snapshots[window_id] = replace(snapshot, state=AgentState.done)
        self.mark_dirty(window_id)
        return True

    def snapshot_for(self, window_id: int) -> AgentSnapshot | None:
        return self.snapshots.get(window_id)


def state_icon(state: AgentState) -> str:
    return {
        AgentState.blocked: '●',
        AgentState.working: '⟳',
        AgentState.done: '✓',
        AgentState.idle: '·',
        AgentState.unknown: '?',
    }[state]


def agent_event_cursor(boss: Any) -> int:
    """Public baseline cursor for future control commands; does not refresh state."""
    return boss.agent_tracker.event_seq


def agent_identity(window: Any, processes: Sequence[Mapping[str, Any]] | None = None) -> tuple[Any, ...]:
    processes = window.child.foreground_processes if processes is None else processes
    agent = detect_agent(processes)
    pid = next((p.get('pid') for p in processes if detect_agent((p,)) == agent), None) if agent else None
    birth = next((p.get('start_time', p.get('create_time', p.get('birth_time'))) for p in processes if pid is not None and p.get('pid') == pid), None)
    return agent, session_id_of_window(window, agent) if agent else '', pid, birth


def state_code(state: AgentState) -> int:
    return {
        AgentState.unknown: 0,
        AgentState.idle: 1,
        AgentState.working: 2,
        AgentState.blocked: 3,
        AgentState.done: 4,
    }[state]


def session_group_for_window(window: Any) -> str:
    age_ns = max(0, time.time_ns() - int(window.created_at))
    if age_ns < 24 * 60 * 60 * 1_000_000_000:
        return 'Today'
    if age_ns < 48 * 60 * 60 * 1_000_000_000:
        return 'Yesterday'
    return 'Earlier'


def group_sidebar_rows(rows: Sequence[AgentSidebarRow], strategy: str) -> tuple[AgentSidebarRow, ...]:
    """Keep each configured group contiguous, preserving row order within it.

    Rows are bucketed by ``group_key`` rather than by the display label, so two
    groups that happen to render the same text stay separate and a group whose
    label changes keeps its identity.
    """
    groups: dict[str, list[AgentSidebarRow]] = {}
    labels: dict[str, str] = {}
    for row in rows:
        if strategy == 'agent' and row.agent:
            row = replace(row, group=row.agent, group_key=row.agent)
        groups.setdefault(row.group_key, []).append(row)
        labels.setdefault(row.group_key, row.group)
    if strategy == 'time':
        rank = {'Today': 0, 'Yesterday': 1, 'Earlier': 2}
        order = tuple(sorted(groups, key=lambda key: (rank.get(labels[key], len(rank)), labels[key].casefold())))
    elif strategy == 'tab':
        order = tuple(groups)
    else:
        order = tuple(sorted(groups, key=lambda key: labels[key].casefold()))
    return tuple(row for key in order for row in groups[key])
