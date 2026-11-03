#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

"""Opt-in OpenCode provider contract, without transport or native dependencies.

AgentService may call normalize_opencode_event with an SDK event, or exchange
encode_opencode_event/parse_opencode_event JSON lines. Version 1 envelopes contain
only the fields declared below; prompts, answers, errors, credentials, transcripts
and tool payloads are deliberately not forwarded. This is not an authenticated
transport: callers must establish trust and session ownership separately.
"""

import json
from collections.abc import Mapping
from typing import Any

OPENCODE_EVENT_VERSION = 1
OPENCODE_CAPABILITIES = frozenset({'session_lifecycle', 'session_status', 'parent_session', 'interaction_id'})
OPENCODE_EVENT_TYPES = frozenset(
    {
        'session.created',
        'session.status',
        'session.idle',
        'permission.asked',
        'permission.replied',
        'question.asked',
        'question.replied',
        'question.rejected',
        'session.error',
        'session.deleted',
    }
)


def opencode_broker_event(event: Mapping[str, Any], event_id: str) -> dict[str, Any] | None:
    """Adapt an SDK event to the broker's protocol_version=1 event message.

    event_id is assigned by the producer (not a provider SDK event identifier).
    Registration uses type=register, protocol_version=1, token, agent=opencode,
    session_id and capabilities=sorted(OPENCODE_CAPABILITIES). Tokens belong only
    in registration and must never be copied into event payloads or logs.
    No command capability, including turn.cancel, is currently implemented.
    """
    if not _identifier(event_id):
        raise ValueError('An event_id is required')
    normalized = normalize_opencode_event(event)
    if normalized is None:
        return None
    value = {'kind': normalized['type'], 'session_id': normalized['session_id'], 'event_id': event_id, 'payload': {}}
    for key in ('parent_session_id', 'interaction_id'):
        if key in normalized:
            value[key] = normalized[key]
    if 'status' in normalized:
        value['payload']['status'] = normalized['status']
    return {'type': 'event', 'event': value}


def _identifier(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def normalize_opencode_event(event: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return a v1 envelope, or None for unsupported/malformed SDK events.

    parent_session_id is present only when info.parentID is supplied; no parent
    or interaction is inferred. Consumers may retain relationships across events.
    A session.error without a session ID cannot be attributed and is ignored.
    """
    event_type = event.get('type')
    if not isinstance(event_type, str) or event_type not in OPENCODE_EVENT_TYPES:
        return None
    properties = event.get('properties')
    if not isinstance(properties, Mapping):
        return None
    info = properties.get('info')
    info = info if isinstance(info, Mapping) else {}
    session_id = _identifier(properties.get('sessionID'))
    if event_type in ('session.created', 'session.deleted'):
        session_id = session_id or _identifier(info.get('id'))
    if session_id is None:
        return None
    result: dict[str, Any] = {
        'version': OPENCODE_EVENT_VERSION,
        'provider': 'opencode',
        'type': event_type,
        'session_id': session_id,
    }
    parent = _identifier(info.get('parentID'))
    if parent is not None:
        result['parent_session_id'] = parent
    state = {
        'session.idle': 'idle',
        'permission.asked': 'blocked',
        'question.asked': 'blocked',
        'permission.replied': 'working',
        'question.replied': 'working',
        'question.rejected': 'working',
        'session.error': 'error',
        'session.deleted': 'clear',
    }.get(event_type)
    if event_type == 'session.status':
        status = properties.get('status')
        status = status.get('type') if isinstance(status, Mapping) else status
        if not isinstance(status, str) or status not in ('busy', 'retry', 'idle'):
            return None
        result['status'] = status
        state = 'idle' if status == 'idle' else 'working'
    if state is not None:
        result['state'] = state
    if event_type.startswith(('permission.', 'question.')):
        result['interaction_kind'] = event_type.split('.')[0]
        key = 'id' if event_type.endswith('.asked') else 'requestID'
        interaction_id = _identifier(properties.get(key))
        if interaction_id is not None:
            result['interaction_id'] = interaction_id
    return result


def encode_opencode_event(event: Mapping[str, Any]) -> str | None:
    """Encode an SDK event as one JSON line; unsupported events return None."""
    normalized = normalize_opencode_event(event)
    if normalized is None:
        return None
    return json.dumps(normalized, ensure_ascii=True, separators=(',', ':')) + '\n'


def parse_opencode_event(line: str | bytes) -> dict[str, Any]:
    """Validate one v1 JSON envelope; raise ValueError on invalid input.

    Unknown fields are rejected, not propagated. This parser performs no I/O.
    """
    try:
        value = json.loads(line)
    except (ValueError, UnicodeError) as err:
        raise ValueError('Invalid OpenCode event JSON') from err
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1 or value.get('provider') != 'opencode':
        raise ValueError('Unsupported OpenCode event envelope')
    properties: dict[str, Any] = {'sessionID': value.get('session_id')}
    if 'parent_session_id' in value:
        properties['info'] = {'parentID': value['parent_session_id']}
    if 'status' in value:
        properties['status'] = value['status']
    event_type = value.get('type')
    if isinstance(event_type, str) and 'interaction_id' in value:
        properties['id' if event_type.endswith('.asked') else 'requestID'] = value['interaction_id']
    normalized = normalize_opencode_event({'type': event_type, 'properties': properties})
    if normalized is None or normalized != value:
        raise ValueError('Invalid OpenCode event fields')
    return normalized
