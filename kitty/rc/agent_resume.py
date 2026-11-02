#!/usr/bin/env python
import json
from typing import Any

from kitty.agent_launch import resume

from .base import ArgsType, Boss, PayloadGetType, PayloadType, RCOptions, RemoteCommand, RemoteControlErrorWithoutTraceback, ResponseType, Window


class AgentResume(RemoteCommand):
    protocol_spec = __doc__ = '''
    agent/str: Explicit manifest agent
    session/str: Explicit session reference
    cwd/str: Explicit absolute working directory
    match/str: Exact source window id selector
    expected_agent/str: Expected live source agent
    expected_session/str: Optional live source session guard
    '''
    short_desc = 'Resume an agent conversation in a controlled new window'
    desc = 'Use --agent --session --cwd OR --match --expected-agent [--expected-session]. No arbitrary launch arguments.'
    options_spec = '''
--agent
Manifest agent name.


--session
Session reference to resume.


--cwd
Existing absolute working directory for explicit mode.


--match
Exact source window id:N.


--expected-agent
Required live source identity in source mode.


--expected-session
Optional live source session guard.
'''
    fields = ('agent', 'session', 'cwd', 'match', 'expected_agent', 'expected_session')
    args = RemoteCommand.Args(spec='', count=0)

    def message_to_kitty(self, global_opts: RCOptions, opts: Any, args: ArgsType) -> PayloadType:
        if args:
            raise RemoteControlErrorWithoutTraceback('No positional arguments allowed')
        return {key: getattr(opts, key) for key in self.fields if getattr(opts, key, None) not in (None, '')}

    def response_from_kitty(self, boss: Boss, window: Window | None, payload_get: PayloadGetType) -> ResponseType:
        # Never copy wire launch options to LaunchCLIOptions. Reject common
        # attempts explicitly; all other unknown fields remain unconsumed.
        for key in ('argv', 'args', 'executable', 'watcher', 'watchers', 'stdin', 'stdin_source', 'env', 'type', 'allow_remote_control'):
            if payload_get(key) is not None:
                raise RemoteControlErrorWithoutTraceback('Arbitrary launch options are forbidden')
        try:
            return json.dumps(self.execute(boss, payload_get), sort_keys=True)
        except ValueError as e:
            raise RemoteControlErrorWithoutTraceback(str(e)) from None

    execute = staticmethod(resume)


agent_resume = AgentResume()
