#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

import json
from time import monotonic
from typing import Any

from kitty.agent_history import HistoryRead
from kitty.agent_service import service_for
from kitty.agents import detect_agent
from kitty.fast_data_types import PRESS, add_timer, send_mouse_event
from kitty.types import AsyncResponse

from .agent_wait import Waiter, pending
from .base import AsyncResponder, RemoteCommand, RemoteControlErrorWithoutTraceback


def resolve_read_target(boss: Any, get: Any) -> tuple[Any, dict[str, Any]]:
    service = service_for(boss)
    expected = get('expected_agent')
    selected = service.resolve_target(get, require_expected=True)
    agent = detect_agent(selected.child.foreground_processes)
    if agent is None or (expected and agent != expected):
        raise ValueError('Target is not the expected live agent')
    identity = service.inspect(selected)
    return selected, dict(window_id=selected.id, agent=agent, **{key: identity[key] for key in ('instance_id', 'runtime_epoch', 'binding_revision')})


def read_terminal(window: Any, source: str, lines: int | None) -> dict[str, Any]:
    if source not in ('visible', 'detection', 'recent', 'recent-unwrapped'):
        raise ValueError('Invalid source')
    recent = source in ('recent', 'recent-unwrapped')
    if lines is not None and (type(lines) is not int or not 1 <= lines <= 10000):
        raise ValueError('lines must be an integer from 1 to 10000')
    if not recent and lines is not None:
        raise ValueError('--lines is only supported for recent sources')
    truncated = False
    if source == 'detection':
        text = '\n'.join(window.agent_detection_snapshot().lines)
    elif source == 'visible':
        text = window.as_text()
    else:
        # History mode selects non-visual live rows. CR marks soft wraps;
        # splitlines() would silently erase the distinction before unwrapping.
        text = window.as_text(add_history=True, add_wrap_markers=True)
        # Limit rendered rows before joining soft wraps, for either source.
        rows = text.replace('\r', '\r\n').split('\n')
        if rows and rows[-1] == '':
            rows.pop()
        limit = 80 if lines is None else lines
        truncated = len(rows) > limit
        text = '\n'.join(rows[-limit:])
        text = text.replace('\r\n', '' if source == 'recent-unwrapped' else '\n').replace('\r', '')
    return dict(
        source=source,
        text=text,
        completeness={
            'scope': 'retained_terminal_history' if recent else 'screen',
            'complete_transcript': False,
            'truncated': truncated,
            'note': 'Terminal history is bounded, may omit alternate-screen output, and is not a complete agent transcript.',
        },
    )


def mouse_guard(screen: Any) -> tuple[Any, str]:
    # Native send_mouse_event checks live tracking on every call and returns
    # False without writing when unsupported. Keyboard flags are an additional
    # continuity guard, not evidence of mouse tracking.
    return screen.current_key_encoding_flags(), ''


def live_state(boss: Any, window: Any) -> str | None:
    service = service_for(boss)
    service.observe(window)
    return service.inspect(window)['execution']['state']


class HistoryWaiter(Waiter):
    def __init__(
        self,
        boss: Any,
        key: str,
        responder: Any,
        window: Any,
        identity: Any,
        fallback: dict[str, Any],
        lines: int | None,
        guard: Any,
        sender: Any = send_mouse_event,
    ) -> None:
        super().__init__(boss, key, {'window_id': None}, responder)
        self.window, self.identity = window, identity
        self.fallback: dict[str, Any] = fallback
        self.guard, self.sender = guard, sender
        self.screen = window.screen
        self.started = monotonic()
        self.cancelled_at = None
        self.sent = 0
        self.reader = HistoryRead(
            self.rows(), self.screen.columns, self.screen.lines, self.started, passive_text=fallback['text'], identity=identity, requested_lines=lines
        )

    def rows(self) -> list[str]:
        rows = []
        for i in range(self.screen.lines):
            line = self.screen.linebuf.line(i)
            rows.append(str(line) + ('\r' if self.screen.linebuf.is_continued(i) else ''))
        return rows

    def cancel(self) -> None:
        if self.cancelled_at is None:
            self.cancelled_at = monotonic()
            self.reader.cancel(self.cancelled_at)

    def poll(self, timer_id: int | None) -> None:
        if self.closed:
            return
        now = monotonic()
        try:
            w = self.boss.window_id_map.get(self.window.id)
            current = None
            valid = False
            if w is self.window:
                _, current = resolve_read_target(self.boss, lambda k: {'target': self.identity['instance_id'], 'expected_agent': self.identity['agent']}.get(k))
                valid = (
                    w.screen is self.screen
                    and self.screen.is_using_alternate_linebuf()
                    and not self.screen.scrolled_by
                    and mouse_guard(self.screen)[0] == self.guard
                    and self.screen.columns == self.reader.columns
                    and self.screen.lines == self.reader.lines
                )
            if now - self.started >= 15:
                self.reader.cancel(now)
            if now - self.started >= 20 or (self.cancelled_at is not None and now - self.cancelled_at >= 5):
                valid = False
            state = live_state(self.boss, w) if valid else None
            action = self.reader.step(
                self.rows(),
                self.screen.columns,
                now,
                valid,
                identity=current,
                working=state not in ('idle', 'done', None, 'unknown'),
                unknown=state in (None, 'unknown'),
            )
            if action.kind == 'wheel':
                for _ in range(action.amount):
                    # Direct protocol injection does not move focus or the real pointer.
                    x, y = self.screen.columns // 2, self.screen.lines // 2
                    geometry = self.window.geometry
                    # Supply valid window-relative pixels even if the child has
                    # selected SGR pixel routing (no mode query is exposed).
                    px = int((x + .5) * (geometry.right - geometry.left) / self.screen.columns)
                    py = int((y + .5) * (geometry.bottom - geometry.top) / self.screen.lines)
                    # Native encoder expects X10 button numbers, not kitty's
                    # internal MOUSE_WHEEL_* constants (which are 8 and 9).
                    if not self.sender(self.screen, x, y,
                                       4 if action.direction == 'up' else 5,
                                       PRESS, 0, px, py):
                        if not self.sent:
                            output: dict[str, Any] = dict(self.fallback, target=self.identity, active_read=False, restoration='not_needed')
                            output['completeness'] = {**output['completeness'], 'reason': 'mouse_mode_unsupported'}
                            self.close(output)
                            return
                        raise ValueError('mouse_send_failed')
                    self.sent += 1
            elif action.kind == 'done':
                result = action.result
                if result is None:
                    raise ValueError('read finished without a result')
                output = dict(self.fallback)
                output['text'] = result.text
                if output['source'] == 'recent-unwrapped' and result.completeness != 'passive':
                    output['text'] = output['text'].replace('\r\n', '').replace('\r', '')
                output['completeness'] = {
                    **output['completeness'],
                    'scope': result.completeness,
                    'reason': result.reason,
                    'truncated': result.truncated,
                }
                output.update(active_read=result.active_read, restoration=result.restoration, concurrency='User input cannot be completely excluded')
                self.close(dict(target=self.identity, **output))
        except Exception:
            output: dict[str, Any] = dict(self.fallback, target=self.identity, active_read=bool(self.sent), restoration='uncertain')
            output['completeness'] = {**output['completeness'], 'reason': 'active_read_failed'}
            self.close(output)


class AgentRead(RemoteCommand):
    is_asynchronous = True
    response_timeout = 25
    args = RemoteCommand.Args(count=0)
    protocol_spec = __doc__ = """
    target/str: Live agent instance id
    match/str: Explicit id:N window selector
    expected_agent/str: Required agent name when using match
    source/choices.visible.detection.recent.recent-unwrapped: Terminal text source
    lines/int: Recent line limit, from 1 to 10000 (default 80)
    passive/bool: Disable active scrolling
    """
    short_desc = 'Read agent terminal text, optionally sending history scroll input'
    desc = """Select --target INSTANCE or --match id:N --expected-agent NAME.
Visible reads the current visual screen, detection the live screen, both in full.
Recent reads retained history plus live rows with soft wraps counted as lines;
recent-unwrapped joins soft wraps after limiting rendered rows. Recent reads may
send mouse wheel input to an idle agent and attempt viewport restoration. Use
--passive to disable this side effect. Remote-control authorization for agent-read
therefore permits mouse input, not just observation. Focus and the real mouse
position are unchanged; concurrent user input cannot be completely excluded.
History is bounded and is not a complete transcript. No agent session files are read."""
    options_spec = """
--target
Live agent instance id.


--match
Explicit id:N window selector.


--expected-agent
Expected live agent name, required with --match.


--source
choices=visible,detection,recent,recent-unwrapped
default=visible
Terminal text source.


--lines
type=int
default=0
Recent sources only: maximum lines, 1..10000, default 80.


--passive
type=bool-set
Disable mouse input and read only retained terminal text.
"""

    def message_to_kitty(self, global_opts: Any, opts: Any, args: Any) -> dict[str, Any]:
        if args:
            self.fatal('agent-read accepts no positional arguments')
        payload = {key: getattr(opts, key) for key in ('target', 'match', 'expected_agent', 'source', 'lines')}
        payload['passive'] = getattr(opts, 'passive', False)
        if payload['lines']:
            try:
                payload['lines'] = int(payload['lines'])
            except (TypeError, ValueError):
                self.fatal('--lines must be an integer from 1 to 10000')
        elif payload['lines'] == 0:
            payload['lines'] = None
        return payload

    def response_from_kitty(self, boss: Any, window: Any, payload_get: Any) -> str | AsyncResponse:
        try:
            # Validate source/limit before touching identity state.
            source, lines = payload_get('source'), payload_get('lines')
            if type(lines) is int and lines == 0:
                lines = None
            if source not in ('visible', 'detection', 'recent', 'recent-unwrapped'):
                raise ValueError('Invalid source')
            if lines is not None and (type(lines) is not int or not 1 <= lines <= 10000 or source not in ('recent', 'recent-unwrapped')):
                raise ValueError('--lines requires a recent source and an integer from 1 to 10000')
            selected, identity = resolve_read_target(boss, payload_get)
            result = read_terminal(selected, source, lines)
            result.update(active_read=False, restoration='not_needed')
            passive = payload_get('passive', missing=False)
            if type(passive) is not bool:
                raise ValueError('passive must be a boolean')
            if source in ('recent', 'recent-unwrapped') and not passive and (lines or 80) > selected.screen.lines:
                reason = ''
                if live_state(boss, selected) not in ('idle', 'done'):
                    reason = 'agent_not_idle'
                    if lines is not None and selected.screen.is_using_alternate_linebuf():
                        raise ValueError(reason)
                elif not selected.screen.is_using_alternate_linebuf():
                    reason = 'not_alternate_screen'
                elif selected.screen.scrolled_by:
                    reason = 'host_scrolled'
                guard, mouse_reason = mouse_guard(selected.screen)
                reason = reason or mouse_reason
                key = payload_get('async_id', missing='')
                if not reason and (not key or key in pending(boss) or len(pending(boss)) >= 16):
                    reason = 'active_read_capacity'
                if not reason and any(isinstance(w, HistoryWaiter) and w.window.id == selected.id for w in pending(boss).values()):
                    reason = 'active_read_busy'
                if not reason:
                    waiter = HistoryWaiter(boss, key, AsyncResponder(payload_get, window), selected, identity, result, lines or 80, guard)
                    pending(boss)[key] = waiter
                    try:
                        waiter.timer = add_timer(waiter.poll, 0.02, True)
                    except Exception:
                        waiter.close()
                        raise
                    return AsyncResponse()
                result['completeness']['reason'] = reason
            return json.dumps(dict(target=identity, **result), sort_keys=True)
        except ValueError as err:
            raise RemoteControlErrorWithoutTraceback(str(err)) from err

    def cancel_async_request(self, boss: Any, window: Any, payload_get: Any) -> None:
        waiter = pending(boss).get(payload_get('async_id', missing=''))
        if isinstance(waiter, HistoryWaiter):
            waiter.cancel()


agent_read = AgentRead()
