#!/usr/bin/env python
from kitty.agent_launch import start

from .agent_resume import AgentResume


class AgentStart(AgentResume):
    protocol_spec = __doc__ = """
    agent/str: Trusted start registry agent
    cwd/str: Existing absolute working directory
    request_id/str: Required short-lived idempotency key
    """
    short_desc = 'Start a trusted agent in a controlled new window'
    desc = 'Requires --agent --cwd --request-id. Only claude, codex, opencode, pi and omp are allowed.'
    options_spec = """
--agent
Trusted start registry agent.


--cwd
Existing absolute working directory.


--request-id
Required idempotency key, retained for at most 60 seconds and 256 entries.
"""
    fields = ('agent', 'cwd', 'request_id')
    execute = staticmethod(start)


agent_start = AgentStart()
