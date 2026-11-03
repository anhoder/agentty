"""One bounded long poll of the service journal, using the RC wait lifecycle."""

import json
from typing import Any

from kitty.agent_service import service_for
from kitty.fast_data_types import add_timer
from kitty.types import AsyncResponse

from .agent_events import AgentEvents
from .agent_wait import Waiter, pending
from .base import AsyncResponder, RemoteControlErrorWithoutTraceback


class JournalWaiter(Waiter):
    def poll(self, timer_id: int | None) -> None:
        service_for(self.boss).poll()
        result = service_for(self.boss).events(self.spec['cursor'], maximum=self.spec['maximum'])
        if result['reason'] != 'timeout':
            self.close(result)

    def timeout(self, timer_id: int | None) -> None:
        self.timer = 0
        self.close(service_for(self.boss).events(self.spec['cursor'], maximum=self.spec['maximum']))

    def close(self, result: Any = None) -> None:
        from kitty.fast_data_types import remove_timer

        if getattr(self, 'poll_timer', 0):
            remove_timer(self.poll_timer)
            self.poll_timer = 0
        super().close(result)


class AgentSubscribe(AgentEvents):
    short_desc = 'Long poll one bounded agent service event batch'
    is_asynchronous = True
    response_timeout = 15
    protocol_spec = AgentEvents.protocol_spec
    options_spec = AgentEvents.options_spec

    def response_from_kitty(self, boss: Any, window: Any, payload_get: Any) -> Any:
        result = json.loads(super().response_from_kitty(boss, window, payload_get))
        timeout = payload_get('timeout_ms', missing=0)
        if type(timeout) is not int or not 0 <= timeout <= 10000:
            raise RemoteControlErrorWithoutTraceback('Invalid timeout_ms')
        key = payload_get('async_id', missing='')
        if result['reason'] != 'timeout' or not timeout or not key:
            return json.dumps(result)
        if key in pending(boss) or len(pending(boss)) >= 16:
            raise RemoteControlErrorWithoutTraceback('Too many pending waits or duplicate async id')
        waiter = JournalWaiter(
            boss, key, {'window_id': None, 'cursor': result['cursor'], 'maximum': payload_get('max_events', missing=100)}, AsyncResponder(payload_get, window)
        )
        pending(boss)[key] = waiter
        try:
            waiter.timer = add_timer(waiter.timeout, timeout / 1000, False)
            waiter.poll_timer = add_timer(waiter.poll, 0.05, True)
        except Exception:
            waiter.close()
            raise
        return AsyncResponse()

    def cancel_async_request(self, boss: Any, window: Any, payload_get: Any) -> None:
        waiter = pending(boss).get(payload_get('async_id', missing=''))
        if waiter is not None:
            waiter.close()


agent_subscribe = AgentSubscribe()
