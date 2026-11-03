#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

import json
from dataclasses import replace
from typing import Any

from kitty.agent_manifest import authority_for, detect
from kitty.agent_service import service_for
from kitty.agents import AgentSnapshot, agent_event_cursor, agent_identity, detect_agent, session_id_of_window, snapshot_for_window, status_scope

from .base import (
    MATCH_WINDOW_OPTION,
    ArgsType,
    Boss,
    PayloadGetType,
    PayloadType,
    RCOptions,
    RemoteCommand,
    RemoteControlErrorWithoutTraceback,
    ResponseType,
    Window,
)


def agent_record(snapshot: AgentSnapshot, window: Window) -> dict[str, Any]:
    return {
        'id': snapshot.window_id,
        'window_id': snapshot.window_id,
        'agent': snapshot.agent,
        'state': snapshot.state.value,
        'title': snapshot.title,
        'cwd': snapshot.cwd,
        'session_id': snapshot.session_id,
        'generation': snapshot.generation,
        'focused': bool(window.is_focused),
    }


class Agent(RemoteCommand):
    protocol_spec = __doc__ = """
    action+/choices.list.get.explain.read.inspect: Read-only operation to perform
    match/str: Window selector using the existing window matching syntax
    self/bool: Select the window the command was run in
    extent/choices.detection.visible: Text source for read
    """
    short_desc = 'Inspect agents without sending input or changing their state'
    desc = """Return stable JSON describing agents. ACTION is list, get, explain, read or inspect.
List defaults to all live windows and omits non-agents. Other actions require exactly
one selected window (the command window or active window by default), and return
JSON null for a non-agent. Use --match id:NUMBER to select a particular window.
Records use window ids as their ids: these are window-level targets, not stable
agent lifecycle ids. Every query checks the current foreground process identity.
Only a cached snapshot of the same agent type supplies the record state; window
metadata is live. Otherwise state comes from live screen/report arbitration with
no previous state. Cached None never hides a newly started agent. Same-type process
restarts cannot be distinguished by this window-level identity check.
Queries never refresh the global tracker or change its dirty/debounce state.
Explain labels the record state source (tracker_cache or live_observation), and
separates live screen evidence/arbitration from the matching cached tracker state
(null when there is no matching cache). Cached state can lag live evidence, even
when the sidebar is disabled; use explain to observe this distinction.
Read defaults to the live detection screen; the visual screen can show scrollback
when scrolled. For bounded terminal history use agent-read. Neither command reads
agent session files or promises a complete conversation transcript."""
    options_spec = (
        MATCH_WINDOW_OPTION
        + """

--self
type=bool-set
Select the window the command is run in, including for list.


--extent
choices=detection,visible
default=detection
Text to return for read. detection is the live screen independent of scroll
position; visible is the current visual screen. Neither includes full scrollback.
"""
    )
    args = RemoteCommand.Args(
        spec='ACTION',
        count=1,
        json_field='action',
        completion=RemoteCommand.CompletionSpec.from_string('type:keyword kwds:list,get,explain,read,inspect'),
    )

    def message_to_kitty(self, global_opts: RCOptions, opts: Any, args: ArgsType) -> PayloadType:
        if len(args) != 1 or args[0] not in ('list', 'get', 'explain', 'read', 'inspect'):
            self.fatal('agent requires one action: list, get, explain, read or inspect')
        return {'action': args[0], 'match': opts.match, 'self': opts.self, 'extent': opts.extent}

    def response_from_kitty(self, boss: Boss, window: Window | None, payload_get: PayloadGetType) -> ResponseType:
        action = payload_get('action')
        if action not in ('list', 'get', 'explain', 'read', 'inspect'):
            raise RemoteControlErrorWithoutTraceback('Invalid agent action: expected list, get, explain, read or inspect')
        extent = payload_get('extent')
        if action == 'read' and extent not in ('detection', 'visible'):
            raise RemoteControlErrorWithoutTraceback('Invalid agent read extent: expected detection or visible')
        if action == 'list' and not payload_get('match') and not payload_get('self'):
            windows = list(boss.all_windows)
        else:
            windows = self.windows_for_match_payload(boss, window, payload_get)
        if action != 'list' and len(windows) != 1:
            raise RemoteControlErrorWithoutTraceback('agent action requires exactly one window; use --match id:NUMBER')

        tracker = boss.agent_tracker

        def snapshot(w: Window) -> AgentSnapshot | None:
            live_agent = detect_agent(w.child.foreground_processes)
            if live_agent is None:
                return None
            session_id = session_id_of_window(w, live_agent)
            cached = tracker.snapshot_for(w.id)
            identity_matches = tracker.identities.get(w.id, agent_identity(w)) == agent_identity(w)
            if cached is not None and cached.agent == live_agent and cached.session_id == session_id and identity_matches:
                # Keep the published state (including debounce/done holds), but
                # never reuse stale window/session metadata from that snapshot.
                current, _ = snapshot_for_window(w, previous=cached.state)
                return replace(current, state=cached.state, generation=cached.generation) if current is not None else None
            # Do not consume dirty flags, advance debounce timers or publish state
            # as a side effect of a read-only remote-control request. In particular
            # do not carry a different agent's previous state into arbitration.
            current = snapshot_for_window(w, w.agent_detection_snapshot())[0]
            generation = tracker.generations.get(w.id, 0) + (0 if w.id in tracker.identities and identity_matches else 1)
            return replace(current, generation=generation) if current is not None else None

        if action == 'list':
            records = [agent_record(s, w) for w in sorted(windows, key=lambda w: w.id) if (s := snapshot(w)) is not None]
            for record in records:
                record['cursor'] = agent_event_cursor(boss)
            return json.dumps(records, sort_keys=True)
        selected = windows[0]
        current = snapshot(selected)
        result: Any = None
        if current is not None:
            result = agent_record(current, selected)
            result['cursor'] = agent_event_cursor(boss)
            if action == 'inspect':
                result.update(service_for(boss).inspect(selected))
            if action == 'explain':
                screen = selected.agent_detection_snapshot()
                match = detect(current.agent, screen)
                candidate, visible_idle = snapshot_for_window(selected, screen, current.state)
                cached = tracker.snapshot_for(selected.id)
                matching_cache = (
                    cached is not None
                    and cached.agent == current.agent
                    and cached.session_id == current.session_id
                    and tracker.identities.get(selected.id, agent_identity(selected)) == agent_identity(selected)
                )
                result = {
                    'record': result,
                    'state_source': 'tracker_cache' if matching_cache else 'live_observation',
                    'tracker_state': cached.state.value if matching_cache else None,
                    'arbitrated_state': candidate.state.value if candidate is not None else None,
                    'authority': authority_for(current.agent).value,
                    'status_scope': status_scope(selected.program_status.as_list(), current.session_id),
                    'visible_idle': visible_idle,
                    'screen': {
                        'source': 'agent_detection_snapshot',
                        'state': match.state.value,
                        'manifest': match.manifest.id if match.manifest is not None else None,
                        'rule': match.rule.id if match.rule is not None else None,
                        'region': match.rule.region if match.rule is not None else None,
                        'priority': match.rule.priority if match.rule is not None else None,
                        'evidence': match.evidence,
                        'skipped': match.skipped,
                    },
                }
            elif action == 'read':
                text = '\n'.join(selected.agent_detection_snapshot().lines) if extent == 'detection' else selected.as_text()
                result = {
                    'record': result,
                    'extent': extent,
                    'source': 'agent_detection_snapshot' if extent == 'detection' else 'window.as_text',
                    'text': text,
                }
        return json.dumps(result, sort_keys=True)


agent = Agent()
