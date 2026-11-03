"""Bounded local provider transport. Only drain() touches kitty objects."""

import json
import os
import secrets
import socket
import tempfile
import threading
import time
from collections import deque
from typing import Any

MAX_LINE_BYTES = 65536
MAX_CLIENTS = 16
MAX_EVENTS = 128
HANDSHAKE_TIMEOUT = 5.0
EVENT_KINDS = frozenset(
    (
        'session.created',
        'session.status',
        'session.idle',
        'session.error',
        'session.deleted',
        'permission.asked',
        'permission.replied',
        'question.asked',
        'question.replied',
        'question.rejected',
        'gap',
    )
)


class Broker:
    def __init__(self, boss: Any, add_timer: Any = None, remove_timer: Any = None) -> None:
        if add_timer is None:
            from kitty.fast_data_types import add_timer, remove_timer
        self.boss = boss
        self.remove_timer = remove_timer
        self.tokens = {}
        self.clients = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.directory = tempfile.mkdtemp(prefix='kitty-agent-')
        os.chmod(self.directory, 0o700)
        self.path = os.path.join(self.directory, 'broker.sock')
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.listener.bind(self.path)
            os.chmod(self.path, 0o600)
            self.listener.listen(MAX_CLIENTS)
            self.listener.setblocking(False)
            self.timer = add_timer(self.drain, 0.05, True)
            self.thread = threading.Thread(target=self.run, name='kitty-agent-broker', daemon=True)
            self.thread.start()
        except BaseException:
            self.listener.close()
            if os.path.exists(self.path):
                os.unlink(self.path)
            os.rmdir(self.directory)
            raise

    def prepare(self, session: Any = None) -> Any:
        if len(self.tokens) >= MAX_EVENTS:
            raise ValueError('Too many managed agent launches')
        token = secrets.token_urlsafe(32)
        self.tokens[token] = [None, session, None, None, time.monotonic() + HANDSHAKE_TIMEOUT]
        return token, [f'KITTY_AGENT_BROKER_SOCKET={self.path}', f'KITTY_AGENT_BROKER_TOKEN={token}']

    def bind(self, token: Any, window: Any) -> None:
        self.tokens[token][0] = window
        agent, _, process = self.identity(window)
        if agent == 'opencode' and process[1] is not None:
            self.tokens[token][3] = process

    def revoke(self, token: Any) -> None:
        self.tokens.pop(token, None)

    def identity(self, window: Any) -> Any:
        from kitty.agents import detect_agent, session_ref_of_window

        processes = window.child.foreground_processes
        agent = detect_agent(processes)
        pid = next((p.get('pid') for p in processes if detect_agent((p,)) == 'opencode'), None)
        return agent, session_ref_of_window(window, 'opencode'), (window.child.pid, pid)

    def live(self, binding: Any) -> bool:
        window = binding[0]
        if window is None or self.boss.window_id_map.get(window.id) is not window:
            return False
        agent, session, process = self.identity(window)
        if agent != 'opencode' or process[1] is None:
            return binding[3] is None and time.monotonic() < binding[4]
        if binding[3] is None:
            binding[3] = process
        if process != binding[3]:
            return False
        return bool(session) or time.monotonic() < binding[4]

    def disconnect(self, c: Any, reason: str) -> None:
        if not c['closed']:
            c['socket'].close()
            c['closed'] = True
            c['reason'] = reason

    def run(self) -> None:
        while not self.stop.wait(0.01):
            with self.lock:
                if len(self.clients) < MAX_CLIENTS:
                    try:
                        sock, _ = self.listener.accept()
                    except (BlockingIOError, OSError):
                        pass
                    else:
                        sock.setblocking(False)
                        self.clients.append(
                            dict(
                                socket=sock,
                                data=bytearray(),
                                events=deque(),
                                closed=False,
                                reason='',
                                token=None,
                                deadline=time.monotonic() + HANDSHAKE_TIMEOUT,
                            )
                        )
                for c in self.clients:
                    if c['closed']:
                        continue
                    if c['token'] is None and time.monotonic() > c['deadline']:
                        self.disconnect(c, 'handshake_timeout')
                        continue
                    try:
                        data = c['socket'].recv(MAX_LINE_BYTES + 1)
                        if not data:
                            self.disconnect(c, 'disconnect')
                            continue
                        c['data'].extend(data)
                        while b'\n' in c['data']:
                            line, _, rest = c['data'].partition(b'\n')
                            c['data'] = bytearray(rest)
                            if len(line) > MAX_LINE_BYTES or len(c['events']) >= MAX_EVENTS:
                                self.disconnect(c, 'gap')
                                break
                            c['events'].append(json.loads(line))
                        if len(c['data']) > MAX_LINE_BYTES:
                            self.disconnect(c, 'gap')
                    except BlockingIOError:
                        pass
                    except (OSError, ValueError, UnicodeError):
                        self.disconnect(c, 'invalid_message')

    def report(self, window: Any, payload: Any) -> Any:
        from kitty.agent_service import service_for

        return service_for(self.boss).report(window, payload)

    def receive(self, c: Any, p: Any) -> None:
        if not isinstance(p, dict):
            raise ValueError('object required')
        if c['token'] is None:
            token = p.get('token')
            binding = self.tokens.get(token) if isinstance(token, str) else None
            if (
                binding is None
                or binding[0] is None
                or binding[2] is not None
                or p.get('type') != 'register'
                or type(p.get('protocol_version')) is not int
                or p.get('protocol_version') != 1
                or set(p) - {'type', 'protocol_version', 'token', 'agent', 'session_id', 'capabilities'}
                or p.get('agent') != 'opencode'
                or 'window_id' in p
            ):
                raise ValueError('invalid registration')
            session = p.get('session_id')
            from kitty.agent_manifest import is_valid_session_id

            if not isinstance(session, str) or not is_valid_session_id(session):
                raise ValueError('invalid session')
            agent, current, process = self.identity(binding[0])
            if not self.live(binding) or agent != 'opencode':
                raise ValueError('process identity not confirmed')
            if current and current != session:
                raise ValueError('live session mismatch')
            if not current and binding[1] is not None and binding[1] != session:
                raise ValueError('session mismatch')
            caps = p.get('capabilities', [])
            if not isinstance(caps, list) or any(not isinstance(x, str) for x in caps):
                raise ValueError('invalid capabilities')
            binding[1], binding[2], c['token'] = session, c, token
            c['sessions'] = {session: None}
            # No command/cancel implementation has been verified yet.
            allowed = {'session_lifecycle', 'session_status', 'parent_session', 'interaction_id'}
            p = dict(p, capabilities=[x for x in caps if x in allowed])
            p.pop('token', None)
        else:
            binding = self.tokens.get(c['token'])
            event = p.get('event')
            if (
                binding is None
                or p.get('type') != 'event'
                or not isinstance(event, dict)
                or set(p) != {'type', 'event'}
                or set(event) - {'kind', 'session_id', 'parent_session_id', 'interaction_id', 'payload', 'event_id'}
                or event.get('kind') not in EVENT_KINDS
                or 'window_id' in p
                or 'window_id' in event
            ):
                raise ValueError('invalid event')
            session = event.get('session_id')
            if not isinstance(session, str):
                raise ValueError('invalid session')
            from kitty.agent_manifest import is_valid_session_id

            if not is_valid_session_id(session):
                raise ValueError('invalid session')
            for field in ('parent_session_id', 'interaction_id'):
                if event.get(field) is not None and (not isinstance(event[field], str) or len(event[field]) > 256):
                    raise ValueError('invalid event identifier')
            if 'event_id' in event and (type(event['event_id']) not in (str, int) or len(str(event['event_id'])) > 256):
                raise ValueError('invalid event id')
            if not isinstance(event.get('payload', {}), dict):
                raise ValueError('invalid payload')
            allowed_payload = (
                {'reason', 'partial', 'children'}
                if event['kind'] == 'gap'
                else {'status', 'snapshot', 'partial'}
                if event['kind'] == 'session.status'
                else set()
            )
            if set(event.get('payload', {})) - allowed_payload:
                raise ValueError('invalid payload fields')
            if event['kind'] == 'gap' and (
                event['payload'].get('partial') is not True
                or event['payload'].get('children') != 'unknown'
                or not isinstance(event['payload'].get('reason'), str)
            ):
                raise ValueError('invalid gap completeness')
            if session not in c['sessions']:
                if event['kind'] != 'session.created' or event.get('parent_session_id') not in c['sessions'] or len(c['sessions']) >= MAX_EVENTS:
                    raise ValueError('untracked session')
                c['sessions'][session] = event['parent_session_id']
            elif event.get('parent_session_id') is not None and event['parent_session_id'] != c['sessions'][session]:
                raise ValueError('parent mismatch')
            if event['kind'] == 'session.deleted':
                removed = {session}
                for _ in range(len(c['sessions'])):
                    removed.update(s for s, parent in c['sessions'].items() if parent in removed)
                for s in removed:
                    c['sessions'].pop(s, None)
        window = binding[0]
        if self.boss.window_id_map.get(window.id) is not window:
            raise ValueError('window no longer live')
        self.report(window, p)

    def drain(self, timer_id: int | None = None) -> None:
        with self.lock:
            for token, binding in list(self.tokens.items()):
                if binding[0] is not None and not self.live(binding):
                    if binding[2] is not None:
                        self.disconnect(binding[2], 'identity_changed')
                    # Keep the binding until its disconnect has revoked service capabilities.
                    else:
                        self.revoke(token)
            for c in list(self.clients):
                binding = self.tokens.get(c['token'])
                if binding and not c['closed']:
                    _, session, _ = self.identity(binding[0])
                    if session and session != binding[1]:
                        self.disconnect(c, 'session_changed')
                        c['events'].clear()
                if c['closed'] and c['reason'] == 'identity_changed':
                    c['events'].clear()
                while c['events']:
                    try:
                        self.receive(c, c['events'].popleft())
                    except Exception:
                        c['events'].clear()
                        self.disconnect(c, 'invalid_message')
                        break
                binding = self.tokens.get(c['token'])
                if binding and self.boss.window_id_map.get(binding[0].id) is not binding[0]:
                    self.disconnect(c, 'window_closed')
                if c['closed']:
                    if binding:
                        if self.boss.window_id_map.get(binding[0].id) is binding[0]:
                            self.report(binding[0], dict(type='gap' if c['reason'] == 'gap' else 'disconnect', reason=c['reason'], resync_required=True))
                        binding[2] = None
                        if c['reason'] == 'identity_changed' or not self.live(binding):
                            self.revoke(c['token'])
                    self.clients.remove(c)
            for token, binding in list(self.tokens.items()):
                if binding[0] is not None and self.boss.window_id_map.get(binding[0].id) is not binding[0]:
                    self.revoke(token)

    def close(self) -> None:
        if self.stop.is_set():
            return
        self.stop.set()
        self.thread.join()
        self.remove_timer(self.timer)
        self.listener.close()
        for c in self.clients:
            c['socket'].close()
        self.clients.clear()
        self.tokens.clear()
        os.unlink(self.path)
        os.rmdir(self.directory)


def broker_for(boss: Any) -> Broker:
    broker = getattr(boss, '_agent_broker', None)
    if broker is None:
        boss._agent_broker = broker = Broker(boss)
    return broker
