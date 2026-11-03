"""Bounded service journal reads."""
import json

from kitty.agent_service import service_for

from .base import RemoteCommand, RemoteControlErrorWithoutTraceback


class AgentEvents(RemoteCommand):
    short_desc = 'Read a bounded epoch-scoped agent event batch'
    protocol_spec = '''cursor/str: JSON epoch cursor
max_events/int: Batch size, at most 100
timeout_ms/int: Long poll timeout
'''
    options_spec = '''--cursor
JSON cursor from a previous response.


--max-events
type=int
default=100
Maximum event count.


--timeout-ms
type=int
default=0
Long poll duration, at most 10000 milliseconds.
'''

    def message_to_kitty(self, global_opts, opts, args):
        if args:
            self.fatal('No positional arguments expected')
        return {k: getattr(opts, k) for k in ('cursor', 'max_events', 'timeout_ms')}

    def response_from_kitty(self, boss, window, payload_get):
        try:
            service_for(boss).poll()
            cursor = payload_get('cursor')
            if isinstance(cursor, str):
                cursor = json.loads(cursor) if cursor else None
            if cursor is not None and not isinstance(cursor, dict):
                raise ValueError('Invalid cursor')
            return json.dumps(service_for(boss).events(cursor, maximum=payload_get('max_events', missing=100)))
        except (ValueError, TypeError) as e:
            raise RemoteControlErrorWithoutTraceback(str(e)) from e


agent_events = AgentEvents()
