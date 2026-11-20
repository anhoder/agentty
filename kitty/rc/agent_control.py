#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

import json
import re
from typing import Any, NoReturn

from kitty.agent_manifest import StateAuthority, authority_for
from kitty.agent_service import service_for
from kitty.agents import AgentState, agent_event_cursor, agent_identity, detect_agent, session_ref_of_window, snapshot_for_window
from kitty.options.utils import parse_shortcut
from kitty.utils import sanitize_for_bracketed_paste

from .base import (
    ArgsType,
    Boss,
    PayloadGetter,
    PayloadGetType,
    PayloadType,
    RCOptions,
    RemoteCommand,
    RemoteControlErrorWithoutTraceback,
    ResponseType,
    Window,
)


def reject(message: str) -> NoReturn:
    raise RemoteControlErrorWithoutTraceback(message)


def validate(payload_get: PayloadGetType) -> tuple[str, list[str], int]:
    match = payload_get('match')
    if not payload_get('target') and (not isinstance(match, str) or re.fullmatch(r'id:[1-9][0-9]*', match) is None):
        reject('agent-control requires an exact positive --match id:NUMBER')
    if not isinstance(payload_get('expected_agent'), str) or not payload_get('expected_agent'):
        reject('--expected-agent is required')
    session = payload_get('expected_session')
    if session is not None and (not isinstance(session, str) or not session):
        reject('--expected-session must be non-empty when supplied')
    action, arguments = payload_get('action'), payload_get('arguments')
    if arguments is None:
        arguments = []
    if action not in ('prompt', 'send-keys', 'focus', 'cancel'):
        reject('Invalid agent-control action')
    if not isinstance(arguments, list) or any(not isinstance(x, str) for x in arguments):
        reject('arguments must be a list of strings')
    if action == 'prompt':
        if len(arguments) != 1 or not arguments[0]:
            reject('prompt requires exactly one non-empty literal text argument')
        text = arguments[0]
        if any((ord(c) < 32 and c not in '\n\t') or 127 <= ord(c) <= 159 for c in text):
            reject('prompt cannot contain terminal control characters')
        try:
            text.encode('utf-8')
        except UnicodeError:
            reject('prompt must be valid UTF-8')
    elif action == 'send-keys':
        if not arguments:
            reject('send-keys requires at least one key')
        for key in arguments:
            try:
                parsed = parse_shortcut(key)
            except Exception:
                reject(f'Invalid key: {key}')
            else:
                if parsed.is_native or not parsed.key:
                    reject(f'Invalid or native key: {key}')
    elif arguments:
        reject(f'{action} takes no arguments')
    if match is not None and (not isinstance(match, str) or re.fullmatch(r'id:[1-9][0-9]*', match) is None):
        reject('match must be exact id:NUMBER')
    return action, arguments, int(match[3:]) if match else 0


class AgentControl(RemoteCommand):
    protocol_spec = __doc__ = """
    action+/str: prompt, send-keys, focus or cancel
    arguments/list.str: Literal prompt text or key names
    match/str: Exact positive window id selector
    target/str: Service instance identity
    expected_agent/str: Required live agent type
    expected_session/str: Optional expected live session reference
    expected_instance/str: Expected service instance identity
    expected_epoch/str: Expected runtime epoch
    expected_binding/int: Expected binding revision
    request_id/str: Idempotency key
    """
    short_desc = 'Send guarded input or focus requests to a live agent'
    desc = """Requires --match id:NUMBER and --expected-agent. prompt takes one literal
text argument and requires live idle state. send-keys takes key names as in send-key.
focus and cancel take no arguments. cancel sends Ctrl+C, never a process signal.
Results acknowledge input or requests, not agent completion or termination."""
    options_spec = """
--target
Service instance identity (alternative to match).


--match
Exact positive window selector id:NUMBER (required).


--expected-agent
Required agent type, checked against live foreground processes.


--expected-session
Expected current session reference, if supplied.


--expected-instance
Expected instance identity.


--expected-epoch
Expected runtime epoch.


--expected-binding
type=int
Expected binding revision.


--request-id
Idempotency key.
"""
    args = RemoteCommand.Args(spec='ACTION [ARGUMENTS ...]', json_field='action', minimum_count=1, first_rest=('action', 'arguments'))

    def message_to_kitty(self, global_opts: RCOptions, opts: Any, args: ArgsType) -> PayloadType:
        payload = {
            'action': args[0] if args else '',
            'arguments': list(args[1:]),
            'match': opts.match,
            'expected_agent': opts.expected_agent,
            'target': getattr(opts, 'target', None),
        }
        if opts.expected_session:
            payload['expected_session'] = opts.expected_session
        for key in ('expected_instance', 'expected_epoch', 'expected_binding', 'request_id'):
            if (value := getattr(opts, key, None)) is not None:
                payload[key] = value
        validate(PayloadGetter(self, payload))
        return payload

    def response_from_kitty(self, boss: Boss, window: Window | None, payload_get: PayloadGetType) -> ResponseType:
        action, arguments, wid = validate(payload_get)
        service = service_for(boss)
        if payload_get('target'):
            try:
                target = service.resolve_target(payload_get, require_expected=True)
            except ValueError as e:
                reject(str(e))
            wid = target.id
        else:
            windows = list(boss.match_windows(payload_get('match')))
            if len(windows) != 1 or windows[0].id != wid:
                reject('agent-control requires exactly one matching window')
            target = windows[0]
        try:
            binding = service.guard(target, payload_get)
            request_id = payload_get('request_id')
            fingerprint = json.dumps([wid, binding['instance_id'], action, arguments, payload_get('expected_agent'), payload_get('expected_session')])
            replay = service.replay(request_id, fingerprint)
            if replay is not None:
                return json.dumps(replay, sort_keys=True)
        except ValueError as e:
            reject(str(e))
        agent = detect_agent(target.child.foreground_processes)
        if agent is None or agent != payload_get('expected_agent'):
            reject('Live foreground agent does not match --expected-agent')
        expected_session = payload_get('expected_session')
        if expected_session is not None and session_ref_of_window(target, agent) != expected_session:
            reject('Live session does not match --expected-session')
        if action == 'prompt':
            published = boss.agent_tracker.observe(target)
            current, visible_idle = boss.agent_tracker.live_snapshot(target)
            reported_idle = any(r.get('state') == 'idle' for r in target.program_status.as_list())
            if (
                current is None
                or current.agent != agent
                or current.state not in (AgentState.idle, AgentState.done)
                or not (current.state == AgentState.done or visible_idle or (authority_for(agent) is StateAuthority.full_lifecycle and reported_idle))
            ):
                reject('prompt requires live idle state')
            baseline_seq = agent_event_cursor(boss)
            data = arguments[0].encode('utf-8')
            if target.screen.in_bracketed_paste_mode:
                data = b'\x1b[200~' + sanitize_for_bracketed_paste(data) + b'\x1b[201~'
            elif '\n' in arguments[0] or '\t' in arguments[0]:
                reject('Multiline text and tabs require live bracketed paste mode')
            target.write_to_child(data)
            target.send_key('enter')
        elif action == 'send-keys':
            target.send_key(*arguments)
        elif action == 'focus':
            boss.set_active_window(target, switch_os_window_if_needed=True)
        else:
            target.send_key('ctrl+c')
        result = {
            'window_id': wid,
            'action': action,
            'baseline_seq': baseline_seq if action == 'prompt' else agent_event_cursor(boss),
            'identity': {
                'agent': agent,
                'session_id': session_ref_of_window(target, agent),
                'foreground_pid': agent_identity(target)[2],
                'generation': published.generation if action == 'prompt' and published else None,
            },
            'input_requested': action in ('prompt', 'send-keys'),
            'focus_requested': action == 'focus',
            'cancel_requested': action == 'cancel',
        }
        result.update({k: binding[k] for k in ('runtime_epoch', 'instance_id', 'binding_revision')})
        if action == 'cancel':
            result['confirmation'] = 'unavailable'
        service.remember(request_id, fingerprint, result)
        return json.dumps(result, sort_keys=True)


agent_control = AgentControl()
