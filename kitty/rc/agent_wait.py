#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>
"""One-shot event batches, optionally waiting on a Boss timer (not a subscription)."""

import json
import re
from typing import Any

from kitty.agents import agent_event_cursor, agent_identity, detect_agent, session_id_of_window
from kitty.fast_data_types import add_timer, remove_timer
from kitty.types import AsyncResponse

from .base import AsyncResponder, RemoteCommand, RemoteControlErrorWithoutTraceback


def pending(boss: Any) -> dict:
    if not hasattr(boss, '_agent_waiters'):
        boss._agent_waiters = {}
    return boss._agent_waiters


def validate(get: Any) -> dict:
    match, agent = get('match'), get('expected_agent')
    if not isinstance(match, str) or not re.fullmatch(r'id:[1-9][0-9]*', match) or not isinstance(agent, str) or not agent.strip():
        raise RemoteControlErrorWithoutTraceback('Require --match id:N and --expected-agent NAME')
    result = {'window_id': int(match[3:]), 'agent': agent, 'session': get('expected_session', None)}
    if result['session'] is not None and not isinstance(result['session'], str):
        raise RemoteControlErrorWithoutTraceback('expected-session must be a string')
    for key, default, low, high in (('after_seq', -1, -1, None), ('timeout_ms', 0, 0, 300000), ('max_events', 100, 1, 100)):
        value = get(key, default)
        if type(value) is not int or value < low or (high is not None and value > high):
            raise RemoteControlErrorWithoutTraceback('Invalid ' + key)
        result[key] = value
    until = get('until', 'changed')
    if not isinstance(until, str) or (
        until != 'changed' and (not until or any(s not in {'idle', 'working', 'blocked', 'done', 'unknown'} for s in until.split(',')))
    ):
        raise RemoteControlErrorWithoutTraceback('Invalid until')
    result['until'] = until
    result['require_working'] = get('require_working', False)
    if type(result['require_working']) is not bool:
        raise RemoteControlErrorWithoutTraceback('Invalid require-working')
    if result['require_working'] and until == 'changed':
        raise RemoteControlErrorWithoutTraceback('require-working requires a state-based until')
    for key in ('working_seq', 'working_generation'):
        value = get(key, None)
        if value is not None and (type(value) is not int or value < (1 if key == 'working_generation' else 0)):
            raise RemoteControlErrorWithoutTraceback('Invalid ' + key)
        result[key] = value
    if (result['working_seq'] is None) != (result['working_generation'] is None):
        raise RemoteControlErrorWithoutTraceback('working-seq and working-generation must be supplied together')
    return result


def batch(boss: Any, spec: dict) -> dict:
    w = boss.window_id_map.get(spec['window_id'])
    if w is None:
        return {
            'reason': 'window_closed',
            'events': [],
            'next_seq': agent_event_cursor(boss),
            'working_seq': spec['working_seq'],
            'working_generation': spec['working_generation'],
        }
    result = boss.agent_tracker.event_batch(spec['window_id'], spec['after_seq'], spec['max_events'])
    if (
        detect_agent(w.child.foreground_processes) != spec['agent']
        or session_id_of_window(w, spec['agent']) != spec['_session']
        or agent_identity(w) != spec['_identity']
    ):
        result['reason'] = 'identity_changed'
    elif result['reason'] == 'changed' and spec['until'] != 'changed':
        matched = False
        for event in result['events']:
            snapshot = event['snapshot']
            if snapshot and snapshot.get('generation') == spec['_generation']:
                if snapshot['state'] == 'working':
                    spec['_working'] = True
                    spec['working_seq'] = event['seq']
                    spec['working_generation'] = spec['_generation']
                if snapshot['state'] in spec['until'].split(',') and (not spec['require_working'] or spec['_working']):
                    matched = True
        if not matched:
            spec['after_seq'] = result['next_seq']
            result['reason'] = 'timeout'
            if result.get('has_more'):
                return batch(boss, spec)
    result.update(working_seq=spec['working_seq'], working_generation=spec['working_generation'])
    return result


class Waiter:
    def __init__(self, boss: Any, key: str, spec: dict, responder: Any) -> None:
        self.boss, self.key, self.spec, self.responder = boss, key, spec, responder
        self.timer = 0
        self.closed = False

    def close(self, result: dict | None = None) -> None:
        if self.closed:
            return
        self.closed = True
        pending(self.boss).pop(self.key, None)
        if self.timer:
            remove_timer(self.timer)
            self.timer = 0
        try:
            if result is not None:
                self.responder.send_data(json.dumps(result))
        except Exception:
            pass  # A disconnected RC peer must not abort Boss lifecycle work.
        finally:
            from kitty.remote_control import active_async_requests

            active_async_requests.pop(self.key, None)
            if not pending(self.boss) and getattr(self.boss, '_agent_observe_timer', 0):
                remove_timer(self.boss._agent_observe_timer)
                self.boss._agent_observe_timer = 0

    def timeout(self, timer_id: int | None) -> None:
        self.timer = 0
        observe_pending(self.boss)
        self.close(batch(self.boss, self.spec))


def observe_pending(boss: Any, timer_id: int | None = None) -> None:
    for wid in {w.spec['window_id'] for w in tuple(pending(boss).values()) if w.spec['window_id'] is not None}:
        window = boss.window_id_map.get(wid)
        if window is not None:
            boss.agent_tracker.observe(window)
        wake_waiters(boss, wid)


def wake_waiters(boss: Any, window_id: int) -> None:
    for waiter in tuple(pending(boss).values()):
        if type(waiter) is Waiter and waiter.spec['window_id'] == window_id:
            result = batch(boss, waiter.spec)
            if result['reason'] != 'timeout':
                waiter.close(result)


def clear_waiters(boss: Any) -> None:
    for waiter in tuple(pending(boss).values()):
        waiter.close()


class AgentWait(RemoteCommand):
    short_desc = 'Wait once for a batch of observed agent changes'
    desc = 'One-shot bounded event batch, not a subscription. Omitted after-seq starts at the current cursor.'
    is_asynchronous = True
    response_timeout = 310
    protocol_spec = """match/str: Exact id:N selector
expected_agent/str: Required agent identity
expected_session/str: Optional session identity
after_seq/int: Event cursor
working_seq/int: Previously observed working event cursor
working_generation/int: Generation of the working event
until/str: changed or comma-separated states
require_working/bool: Require observed working before a terminal state
 timeout_ms/int: Wait duration, at most 300000 ms
max_events/int: Batch limit, at most 100
"""
    options_spec = """--match
Exact id:N selector (required).


--expected-agent
Expected agent name (required).


--expected-session
Expected session identity.


--after-seq
type=int
default=-1
Event cursor (nonnegative).


--until
default=changed
changed or comma-separated states.


--working-seq
type=int
Previously observed working event cursor.


--working-generation
type=int
Generation of the working event.


--require-working
type=bool-set
Require a working event after the cursor in this generation.


--timeout-ms
type=int
default=0
Maximum wait in milliseconds, 0..300000.


--max-events
type=int
default=100
Maximum events, 1..100.
"""

    def message_to_kitty(self, global_opts: Any, opts: Any, args: Any) -> dict:
        if args:
            self.fatal('agent-wait takes no positional arguments')
        payload = {
            k: getattr(opts, k)
            for k in (
                'match',
                'expected_agent',
                'expected_session',
                'after_seq',
                'timeout_ms',
                'max_events',
                'until',
                'require_working',
                'working_seq',
                'working_generation',
            )
        }
        validate(payload.get)
        return payload

    def response_from_kitty(self, boss: Any, window: Any, payload_get: Any) -> Any:
        def get(key: Any, default: Any = None) -> Any:
            return payload_get(key, missing=default)

        spec = validate(get)
        cursor = agent_event_cursor(boss)
        if spec['after_seq'] == -1:
            spec['after_seq'] = cursor
        if spec['after_seq'] > cursor:
            raise RemoteControlErrorWithoutTraceback('after-seq is ahead of the current cursor')
        w = boss.window_id_map.get(spec['window_id'])
        if w is not None:
            boss.agent_tracker.observe(w)
        spec['_session'] = spec['session'] if spec['session'] is not None else (session_id_of_window(w, spec['agent']) if w else '')
        spec['_identity'] = agent_identity(w) if w else None
        cached = boss.agent_tracker.snapshot_for(spec['window_id'])
        spec['_generation'] = cached.generation if cached else 0
        if w is not None and spec['working_seq'] is not None:
            if spec['working_seq'] > spec['after_seq'] or spec['working_generation'] != spec['_generation']:
                raise RemoteControlErrorWithoutTraceback('working cursor/generation does not match this request')
        spec['_working'] = spec['working_seq'] is not None
        result = batch(boss, spec)
        if result['reason'] != 'timeout' or not spec['timeout_ms']:
            return json.dumps(result)
        key = get('async_id', '')
        if not key:
            return json.dumps(result)
        if key in pending(boss) or len(pending(boss)) >= 16:
            raise RemoteControlErrorWithoutTraceback('Too many pending agent waits or duplicate async id')
        waiter = Waiter(boss, key, spec, AsyncResponder(payload_get, window))
        pending(boss)[key] = waiter
        try:
            waiter.timer = add_timer(waiter.timeout, spec['timeout_ms'] / 1000, False)
            if not getattr(boss, '_agent_observe_timer', 0):
                boss._agent_observe_timer = add_timer(lambda tid: observe_pending(boss, tid), 0.05, True)
        except Exception:
            waiter.close()
            raise
        return AsyncResponse()

    def cancel_async_request(self, boss: Any, window: Any, payload_get: Any) -> None:
        waiter = pending(boss).get(payload_get('async_id', missing=''))
        if waiter is not None:
            waiter.close()


agent_wait = AgentWait()
