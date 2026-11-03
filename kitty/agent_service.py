"""Small main-loop-owned agent identity, provider and event boundary (no native imports)."""
import json
from copy import deepcopy
from collections import OrderedDict, deque
from uuid import uuid4


class AgentService:
    def __init__(self, boss, max_events=256, max_bytes=262144):
        self.boss = boss
        self.runtime_epoch = uuid4().hex
        self.max_events, self.max_bytes = max_events, max_bytes
        self.seq = self.event_bytes = 0
        self.journal = deque()
        self.bindings = {}
        self.providers = {}
        self.requests = OrderedDict()
        self.observations = {}

    def poll(self):
        live = set()
        for window in self.boss.all_windows:
            live.add(window.id)
            self.observe(window)
        for wid in set(self.bindings) - live:
            self.emit(wid, 'removed', {'instance_id': self.bindings[wid]['instance_id']})
            self.bindings.pop(wid, None)
            self.providers.pop(wid, None)
            self.observations.pop(wid, None)

    def observe(self, window):
        """Boss refresh hook; caller must only call on an already-created service.

        Reads live evidence without refreshing/consuming tracker state. Journal
        coverage is observed transitions, not a claim of complete provider history.
        """
        from kitty.agents import snapshot_for_window
        binding = self.binding(window)
        snapshot = snapshot_for_window(window, window.agent_detection_snapshot())[0]
        state = {'instance_id': binding['instance_id'], 'state': snapshot.state.value if snapshot else None,
                 'state_source': 'live_observation'}
        if self.observations.get(window.id) != state:
            self.observations[window.id] = state
            self.emit(window.id, 'state', state)
        return deepcopy(state)

    def binding(self, window):
        from kitty.agents import agent_identity
        identity = agent_identity(window)
        if len(identity) == 3:
            processes = getattr(getattr(window, 'child', None), 'foreground_processes', ())
            birth = next((p.get('start_time', p.get('create_time', p.get('birth_time')))
                          for p in processes if p.get('pid') == identity[2]), None)
            identity = (*identity, birth)
        old = self.bindings.get(window.id)
        if old is None or old['identity'] != identity:
            revision = old['binding_revision'] + 1 if old else 1
            old = dict(identity=identity, instance_id=uuid4().hex, binding_revision=revision)
            self.bindings[window.id] = old
            self.providers.pop(window.id, None)
            self.emit(window.id, 'binding', {k: v for k, v in old.items() if k != 'identity'})
        return dict(old, runtime_epoch=self.runtime_epoch)

    def emit(self, window_id, kind, data):
        # Copy at the boundary: callers cannot mutate retained events.
        data = json.loads(json.dumps(data))
        event = dict(seq=self.seq + 1, window_id=window_id, type=kind, data=data)
        size = len(json.dumps(event).encode())
        self.seq += 1
        if size > self.max_bytes:
            self.journal.clear()
            self.event_bytes = 0
            return
        self.journal.append((event, size))
        self.event_bytes += size
        while len(self.journal) > self.max_events or self.event_bytes > self.max_bytes:
            self.event_bytes -= self.journal.popleft()[1]

    def inspect(self, window):
        binding = self.binding(window)
        provider = self.providers.get(window.id, {})
        return deepcopy({k: v for k, v in binding.items() if k != 'identity'} | {
            'schema_version': 1,
            'runtime': {'epoch': self.runtime_epoch, 'persistence': 'memory', 'event_coverage': 'observed_transitions'},
            'execution': {'source': 'provider_report' if provider.get('connected') and provider.get('state') else 'live_observation',
                          'state': provider.get('state') if provider.get('connected') and provider.get('state')
                          else self.observations.get(window.id, {}).get('state')},
            'presentation': {'source': 'tracker_cache',
                             'state': self._presentation_state(window.id)},
            'capabilities': {'terminal': ['read', 'focus', 'send-keys', 'prompt', 'cancel'],
                             'provider': list(provider.get('capabilities', [])),
                             'unsupported': [c for c in ('transcript', 'tool', 'approve', 'answer', 'cancel_confirm')
                                             if c not in provider.get('capabilities', [])]},
            'provider': provider or None,
            'state_source': 'provider_report' if provider.get('connected') and provider.get('state') else 'live_observation',
            'provider_state': provider.get('state') if provider.get('connected') else None,
            'cursor': self.cursor()})

    def _presentation_state(self, window_id):
        tracker = getattr(self.boss, 'agent_tracker', None)
        snapshot = tracker.snapshot_for(window_id) if tracker is not None else None
        return snapshot.state.value if snapshot is not None else None

    def cursor(self):
        return {'runtime_epoch': self.runtime_epoch, 'seq': self.seq}

    def events(self, cursor=None, window_id=None, maximum=100):
        if type(maximum) is not int or not 1 <= maximum <= 100:
            raise ValueError('max_events must be 1..100')
        cursor = self.cursor() if cursor is None else cursor
        seq = cursor.get('seq')
        if type(seq) is not int or seq < 0:
            raise ValueError('Invalid cursor')
        floor = self.journal[0][0]['seq'] - 1 if self.journal else self.seq
        if cursor.get('runtime_epoch') != self.runtime_epoch or seq < floor or seq > self.seq:
            return dict(reason='resync', resync_required=True, events=[], cursor=self.cursor(),
                        oldest_seq=floor + 1, head_seq=self.seq,
                        snapshot=deepcopy({'bindings': {wid: {k: v for k, v in b.items() if k != 'identity'} for wid, b in self.bindings.items()},
                                           'observations': self.observations, 'providers': self.providers}))
        events = [deepcopy(e) for e, _ in self.journal if e['seq'] > seq and (window_id is None or e['window_id'] == window_id)]
        more = len(events) > maximum
        events = events[:maximum]
        return dict(reason='changed' if events else 'timeout', resync_required=False, events=events, oldest_seq=floor + 1, head_seq=self.seq,
                    has_more=more, cursor={'runtime_epoch': self.runtime_epoch,
                                          'seq': events[-1]['seq'] if more else self.seq})

    def report(self, window, payload):
        binding = self.binding(window)
        kind = payload.get('type')
        if kind == 'register':
            caps = payload.get('capabilities', [])
            if not isinstance(caps, list) or any(not isinstance(c, str) for c in caps):
                raise ValueError('capabilities must be strings')
            self.providers[window.id] = {'provider_id': payload.get('provider_id'), 'capabilities': sorted(set(caps)),
                                         'connected': True, 'children': {}, 'interactions': {}}
            payload = {'type': kind, 'capabilities': sorted(set(caps))}
        elif kind in ('disconnect', 'gap'):
            provider = self.providers.get(window.id, {})
            if provider.get('state') is not None:
                provider['last_known_state'] = provider['state']
            provider['stale'] = True
            provider['completeness'] = {'partial': True, 'children': 'unknown', 'interactions': 'unknown'}
            provider.update(connected=False, capabilities=[], children={}, interactions={}, state=None)
            payload = {'type': kind}
        elif kind == 'event':
            event = payload.get('event', {})
            if not isinstance(event, dict):
                raise ValueError('event must be an object')
            if not isinstance(event.get('kind'), str) or not event['kind'] or len(event['kind']) > 128:
                raise ValueError('Invalid event kind')
            allowed = {'session.created', 'session.status', 'session.idle', 'session.error', 'session.deleted',
                       'permission.asked', 'permission.replied', 'question.asked', 'question.replied', 'question.rejected', 'gap'}
            if event['kind'] not in allowed:
                raise ValueError('Unsupported event kind')
            status = None
            body = event.get('payload', {})
            if not isinstance(body, dict):
                raise ValueError('Invalid event payload')
            completeness = {}
            for flag in ('snapshot', 'partial'):
                if flag in body:
                    if type(body[flag]) is not bool:
                        raise ValueError('Invalid completeness flag')
                    completeness[flag] = body[flag]
            if event['kind'] == 'gap':
                if body.get('children') != 'unknown' or body.get('partial') is not True:
                    raise ValueError('Invalid gap completeness')
                reason = body.get('reason')
                if not isinstance(reason, str) or not reason or len(reason) > 128:
                    raise ValueError('Invalid gap reason')
                completeness.update(reason=reason, children='unknown')
            if event['kind'] == 'session.status':
                status = body.get('status') if isinstance(body, dict) else None
                if status not in ('busy', 'retry', 'idle'):
                    raise ValueError('Invalid session status')
            for field in ('session_id', 'parent_session_id', 'interaction_id', 'event_id'):
                if field in event and (not isinstance(event[field], str) or not event[field] or len(event[field]) > 512):
                    raise ValueError('Invalid event ' + field)
            if event.get('parent_session_id') == event.get('session_id') and event.get('session_id'):
                raise ValueError('Session cannot parent itself')
            # Never journal transport credentials or arbitrary provider payloads.
            event = {k: event[k] for k in ('kind', 'session_id', 'parent_session_id', 'interaction_id', 'event_id')
                     if isinstance(event.get(k), str)}
            if status is not None or completeness:
                event['payload'] = dict(completeness)
                if status is not None:
                    event['payload']['status'] = status
            provider = self.providers.get(window.id)
            if provider is None or not provider.get('connected'):
                raise ValueError('Provider not registered')
            if completeness:
                provider['completeness'] = dict(completeness)
            if event['kind'] == 'gap':
                if provider.get('state') is not None:
                    provider['last_known_state'] = provider['state']
                provider.update(children={}, interactions={}, state=None, stale=True)
                provider['completeness'].update(interactions='unknown')
            if event['kind'] == 'session.status' and event.get('session_id') == binding['identity'][1]:
                provider['state'] = 'idle' if status == 'idle' else 'working'
                provider['stale'] = False
            elif event['kind'] == 'session.idle' and event.get('session_id') == binding['identity'][1]:
                provider['state'] = 'idle'
                provider['stale'] = False
            elif event['kind'].endswith('.asked') and event.get('session_id') == binding['identity'][1]:
                provider['state'] = 'blocked'
                provider['stale'] = False
            if event.get('parent_session_id') and event.get('session_id'):
                provider['children'][event['session_id']] = event['parent_session_id']
            if event.get('interaction_id'):
                if event['kind'].endswith('.asked'):
                    provider['interactions'][event['interaction_id']] = {'kind': event['kind'], 'session_id': event.get('session_id')}
                elif event['kind'].endswith(('.replied', '.rejected')):
                    provider['interactions'].pop(event['interaction_id'], None)
            if event['kind'] == 'session.deleted':
                removed = {event.get('session_id')}
                while True:
                    children = {child for child, parent in provider['children'].items() if parent in removed}
                    if children <= removed:
                        break
                    removed.update(children)
                provider['children'] = {child: parent for child, parent in provider['children'].items() if child not in removed}
                provider['interactions'] = {key: value for key, value in provider['interactions'].items() if value['session_id'] not in removed}
            for field in ('children', 'interactions'):
                while len(provider[field]) > 128:
                    provider[field].pop(next(iter(provider[field])))
            payload = {'type': kind, 'event': event}
        else:
            raise ValueError('Invalid provider report type')
        self.emit(window.id, 'provider_' + kind, payload)
        return self.inspect(window)

    def resolve_target(self, get, require_expected=False):
        """Resolve an instance ID or exact window selector and enforce live guards."""
        import re
        from kitty.agents import detect_agent, session_ref_of_window
        boss = self.boss
        target, match = get('target'), get('match')
        if target:
            windows = [w for w in boss.all_windows if self.binding(w)['instance_id'] == target]
            if match and (len(windows) != 1 or match != f'id:{windows[0].id}'):
                raise ValueError('target and match disagree')
        else:
            if not isinstance(match, str) or not re.fullmatch(r'id:[1-9][0-9]*', match):
                raise ValueError('Require --target or exact --match id:N and --expected-agent')
            windows = list(boss.match_windows(match))
        if require_expected and not target and not get('expected_agent'):
            raise ValueError('--expected-agent is required')
        if len(windows) != 1:
            raise ValueError('Require exactly one live target')
        window = windows[0]
        self.guard(window, get)
        agent = detect_agent(window.child.foreground_processes)
        if not agent or (get('expected_agent') and agent != get('expected_agent')):
            raise ValueError('Live foreground agent does not match --expected-agent')
        if get('expected_session') is not None and session_ref_of_window(window, agent) != get('expected_session'):
            raise ValueError('Live session does not match --expected-session')
        return window

    def guard(self, window, get):
        binding = self.binding(window)
        for key, field in (('expected_instance', 'instance_id'), ('expected_epoch', 'runtime_epoch'),
                           ('expected_binding', 'binding_revision')):
            value = get(key)
            if value is not None and value != binding[field]:
                raise ValueError(key + ' does not match live binding')
        return binding

    def replay(self, request_id, fingerprint):
        if request_id is None:
            return None
        if not isinstance(request_id, str) or not request_id or len(request_id) > 256:
            raise ValueError('Invalid request_id')
        old = self.requests.get(request_id)
        if old:
            if old[0] != fingerprint:
                raise ValueError('request_id reused with different request')
            return deepcopy(old[1])

    def remember(self, request_id, fingerprint, result):
        if request_id is not None:
            self.requests[request_id] = (fingerprint, deepcopy(result))
        while len(self.requests) > 256:
            self.requests.popitem(last=False)
        return result


def service_for(boss):
    service = getattr(boss, '_agent_service', None)
    if service is None:
        service = boss._agent_service = AgentService(boss)
    return service
