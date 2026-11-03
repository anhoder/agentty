"""Guarded terminal prompt submission with a bounded activity/settlement wait."""

import json
from time import monotonic
from typing import Any

from kitty.agent_service import service_for
from kitty.agents import detect_agent
from kitty.fast_data_types import add_timer
from kitty.types import AsyncResponse

from .agent_control import agent_control, reject
from .agent_wait import Waiter, pending
from .base import AsyncResponder, PayloadGetter, RemoteCommand


class PromptWaiter(Waiter):
    def poll(self, timer_id: int | None) -> None:
        service = service_for(self.boss)
        now = monotonic()
        result = dict(self.spec['result'])
        reason = None
        # The total deadline takes precedence over the activity gate.
        if now >= self.spec['deadline']:
            reason = 'timeout'
        else:
            window = self.boss.window_id_map.get(self.spec['window_id'])
            if window is None:
                reason = 'window_closed'
            elif service.binding(window)['instance_id'] != result['instance_id']:
                reason = 'identity_changed'
            else:
                service.observe(window)
                batch = service.events(self.spec['cursor'], window.id)
                while batch.get('has_more'):
                    page = service.events(batch['cursor'], window.id)
                    batch['events'].extend(page['events'])
                    batch.update({k: v for k, v in page.items() if k != 'events'})
                self.spec['cursor'] = batch['cursor']
                if batch['resync_required']:
                    reason = 'gap'
                for event in batch['events']:
                    data = event['data']
                    if event['type'] in ('removed', 'provider_gap', 'provider_disconnect') or (
                        event['type'] == 'provider_event' and data.get('event', {}).get('kind') == 'gap'
                    ):
                        reason = 'gap'
                        break
                    state = data.get('state') if event['type'] == 'state' else None
                    if event['type'] == 'provider_event':
                        report = data.get('event', {})
                        if report.get('session_id') == service.binding(window)['identity'][1]:
                            kind = report.get('kind')
                            if kind == 'session.status':
                                state = 'idle' if report.get('payload', {}).get('status') == 'idle' else 'working'
                            elif kind == 'session.idle':
                                state = 'idle'
                            elif kind in ('permission.asked', 'question.asked'):
                                state = 'blocked'
                    if state in ('working', 'blocked'):
                        self.spec['active'] = True
                        if state == 'working':
                            self.spec['result']['working_seq'] = result['working_seq'] = event['seq']
                    if state in self.spec['until'] and self.spec['active']:
                        reason = 'settled'
                        result['state'] = state
                        result['completion_seq'] = event['seq']
                        break
                    if event['type'] == 'state' and state in (None, 'unknown'):
                        reason = 'unknown'
                if reason is None and not self.spec['active'] and now >= self.spec['activity_deadline']:
                    reason = 'stalled'
        if reason:
            result['reason'] = reason
            result['status'] = (
                'completed' if reason == 'settled' else ('unknown' if reason in ('gap', 'unknown', 'identity_changed', 'window_closed') else reason)
            )
            self.close(result)

    def close(self, result: Any = None) -> None:
        from kitty.fast_data_types import remove_timer

        if getattr(self, 'poll_timer', 0):
            remove_timer(self.poll_timer)
            self.poll_timer = 0
        super().close(result)


class AgentPrompt(RemoteCommand):
    short_desc = 'Submit a guarded terminal prompt and optionally wait for settlement'
    desc = """Requires live idle or done; working is refused to avoid injecting into an
active turn. blocked and unknown are refused. Wait requires post-submit activity;
startup idle is not completion. No automatic retry or provider-native commands."""
    is_asynchronous = True
    response_timeout = 310
    protocol_spec = """text+/str: Literal prompt text
target/str: Service instance identity
match/str: Exact id:N selector
expected_agent/str: Expected live agent name
expected_session/str: Expected session
expected_instance/str: Expected instance
expected_epoch/str: Expected runtime epoch
expected_binding/int: Expected binding revision
request_id/str: Bounded submission deduplication key
wait/bool: Wait for post-submit activity and settlement
timeout_ms/int: Total timeout, 1..300000 milliseconds
until/str: Comma-separated idle,done,blocked settlement states
"""
    options_spec = (
        (agent_control.options_spec or '')
        + """


--wait
type=bool-set
Wait for observed activity followed by settlement.


--timeout-ms
type=int
default=300000
Total wait timeout in milliseconds, 1..300000.


--until
default=idle,done,blocked
Comma-separated settlement states: idle, done, blocked.
"""
    )
    args = RemoteCommand.Args(spec='TEXT', json_field='text', count=1)

    def message_to_kitty(self, global_opts: Any, opts: Any, args: Any) -> dict:
        result = {
            k: getattr(opts, k, None)
            for k in (
                'target',
                'match',
                'expected_agent',
                'expected_session',
                'expected_instance',
                'expected_epoch',
                'expected_binding',
                'request_id',
                'wait',
                'timeout_ms',
                'until',
            )
        }
        result['text'] = args[0]
        return result

    def response_from_kitty(self, boss: Any, window: Any, payload_get: Any) -> Any:
        def get(key: str) -> Any:
            return payload_get(key, missing=None)

        wait = get('wait')
        if wait is None:
            wait = False
        if type(wait) is not bool:
            reject('wait must be a boolean')
        until = get('until') or 'idle,done,blocked'
        if not isinstance(until, str) or any(s not in ('idle', 'done', 'blocked') for s in until.split(',')):
            reject('until must contain comma-separated idle,done,blocked states')
        timeout = get('timeout_ms')
        timeout = 300000 if timeout is None else timeout
        if type(timeout) is not int or not 1 <= timeout <= 300000:
            reject('timeout-ms must be 1..300000')
        service = service_for(boss)
        try:
            target = service.resolve_target(get)
        except ValueError as e:
            reject(str(e))
        key = get('async_id')
        if wait and (not key or key in pending(boss) or len(pending(boss)) >= 16):
            reject('Wait requires a unique async request and available waiter capacity')
        # Observe and capture the journal baseline before any input is written.
        observed = service.observe(target)
        cursor = service.cursor()
        execution = service.inspect(target)['execution']['state']
        if observed['state'] not in ('idle', 'done') or execution not in ('idle', 'done'):
            reject('prompt requires idle/done; working is refused for safety, blocked/unknown cannot accept prompts')
        payload = {k: get(k) for k in ('expected_session', 'expected_instance', 'expected_epoch', 'expected_binding', 'request_id')}
        payload.update(action='prompt', arguments=[get('text')], match=f'id:{target.id}', expected_agent=detect_agent(target.child.foreground_processes))
        response = agent_control.response_from_kitty(boss, window, PayloadGetter(agent_control, payload))
        if not isinstance(response, str):
            reject('Unexpected agent-control response')
        result = json.loads(response)
        result['baseline_cursor'] = cursor
        result['tracker_baseline_seq'] = result['baseline_seq']
        result['baseline_seq'] = cursor['seq']
        result.update(
            status='submitted',
            reason='input_requested',
            working_seq=None,
            completion_seq=None,
            observation='Observed transitions, not confirmation of a provider turn',
        )
        if not wait:
            return json.dumps(result)
        now = monotonic()
        waiter = PromptWaiter(
            boss,
            key,
            dict(
                window_id=target.id,
                cursor=cursor,
                result=result,
                active=False,
                until=until.split(','),
                deadline=now + timeout / 1000,
                activity_deadline=now + 5,
            ),
            AsyncResponder(payload_get, window),
        )
        pending(boss)[key] = waiter
        try:
            waiter.poll_timer = add_timer(waiter.poll, 0.05, True)
        except Exception:
            waiter.close()
            raise
        return AsyncResponse()

    def cancel_async_request(self, boss: Any, window: Any, payload_get: Any) -> None:
        waiter = pending(boss).get(payload_get('async_id', missing=''))
        if waiter is not None:
            waiter.close()


agent_prompt = AgentPrompt()
