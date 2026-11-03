"""Restricted agent launch policy. No user supplied launch options are forwarded."""

import os
import re
import time
from collections import OrderedDict
from threading import RLock
from typing import Any, Never

from kitty.agent_manifest import is_valid_session_id, is_valid_session_path
from kitty.agents import detect_agent, resume_argv_for, session_ref_of_window
from kitty.launch import launch, parse_launch_args

START_ARGV = {name: (name,) for name in ('claude', 'codex', 'opencode', 'pi', 'omp')}
REQUEST_TTL = 60.0
REQUEST_LIMIT = 256
_requests = OrderedDict()
_lock = RLock()


def reject(message: str) -> Never:
    raise ValueError(message)


def cwd_directory(value: Any) -> str:
    if not isinstance(value, str) or not os.path.isabs(value) or not os.path.isdir(value):
        reject('cwd must be an existing absolute directory')
    return value


def source_window(boss: Any, match: Any) -> Any:
    if not isinstance(match, str) or re.fullmatch(r'id:[1-9][0-9]*', match) is None:
        reject('source requires exact --match id:N')
    windows = list(boss.match_windows(match))
    if len(windows) != 1 or windows[0].id != int(match[3:]):
        reject('source must match exactly one window')
    return windows[0]


def controlled_launch(boss: Any, source: Any, cwd: str, argv: Any, agent: Any = None, session: Any = None) -> dict[str, Any]:
    tab = source.tabref() if source is not None else boss.active_tab
    if tab is None:
        reject('No target tab')
    opts = parse_launch_args([])[0]
    opts.type = 'window'
    opts.cwd = cwd
    opts.source_window = f'id:{source.id}' if source is not None else ''
    opts.copy_env = False
    opts.copy_cmdline = False
    opts.stdin_source = 'none'
    opts.watcher = []
    broker = token = None
    if agent == 'opencode':
        from kitty.agent_broker import broker_for

        broker = broker_for(boss)
        token, opts.env = broker.prepare(session)
    try:
        w = launch(boss, opts, list(argv), target_tab=tab, force_target_tab=True)
    except BaseException:
        if broker is not None:
            broker.revoke(token)
        raise
    if w is None:
        if broker is not None:
            broker.revoke(token)
        reject('Launch returned no window; no automatic cleanup or retry was performed')
    if broker is not None:
        broker.bind(token, w)
    return {'launch_requested': True, 'window_id': w.id, 'stage': 'launch_requested', 'identity_confirmed': False}


def resume(boss: Any, get: Any) -> Any:
    match, expected, expected_session = get('match'), get('expected_agent'), get('expected_session')
    agent, session, cwd = get('agent'), get('session'), get('cwd')
    source = None
    if match is not None or expected is not None or expected_session is not None:
        if agent is not None or session is not None or cwd is not None:
            reject('Explicit agent/session/cwd and guarded source modes are mutually exclusive')
        source = source_window(boss, match)
        agent = detect_agent(source.child.foreground_processes)
        if not isinstance(expected, str) or not expected or agent != expected:
            reject('Live source agent does not match expected-agent')
        session = session_ref_of_window(source, agent)
        if expected_session is not None and (not isinstance(expected_session, str) or not expected_session or session != expected_session):
            reject('Live source session does not match expected-session')
        cwd = source.cwd_of_child or source.child.cwd
    if not isinstance(agent, str) or not isinstance(session, str):
        reject('agent and session are required')
    if not (is_valid_session_id(session) or (agent in ('pi', 'omp') and is_valid_session_path(session))):
        reject('Invalid session reference')
    cwd = cwd_directory(cwd)
    argv = resume_argv_for(agent, session)
    if not argv:
        reject('No manifest resume command for agent/session')
    with _lock:
        for w in boss.window_id_map.values():
            # A source is a deliberate request to fork its conversation into a
            # new window; it is not counted as an accidental duplicate itself.
            if w is source:
                continue
            if detect_agent(w.child.foreground_processes) == agent and session_ref_of_window(w, agent) == session:
                reject('Session already active in another window')
        now = time.monotonic()
        key = ('resume', id(boss), agent, session)
        if key in _requests and now - _requests[key][0] < REQUEST_TTL:
            reject('Session launch recently requested')
        result = controlled_launch(boss, source, cwd, argv, agent, session)
        remember(key, now, (agent, session, cwd), result)
        return result


def remember(key: Any, now: float, fingerprint: Any, result: Any) -> None:
    for old in list(_requests):
        if now - _requests[old][0] >= REQUEST_TTL:
            del _requests[old]
    _requests[key] = (now, fingerprint, dict(result))
    while len(_requests) > REQUEST_LIMIT:
        _requests.popitem(last=False)


def start(boss: Any, get: Any) -> Any:
    agent, cwd, request_id = get('agent'), cwd_directory(get('cwd')), get('request_id')
    if not isinstance(agent, str) or agent not in START_ARGV:
        reject('Agent not in trusted start registry')
    if not isinstance(request_id, str) or re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', request_id) is None:
        reject('request-id must contain 1-128 ASCII letters, digits, _, ., :, or -')
    fingerprint = (agent, cwd)
    with _lock:
        now = time.monotonic()
        key = ('start', id(boss), request_id)
        previous = _requests.get(key)
        if previous is not None and now - previous[0] < REQUEST_TTL:
            if previous[1] != fingerprint:
                reject('request-id reused with different parameters')
            return dict(previous[2], replayed=True)
        result = controlled_launch(boss, None, cwd, START_ARGV[agent], agent)
        result.update(request_id=request_id, replayed=False)
        remember(key, now, fingerprint, result)
        return result
