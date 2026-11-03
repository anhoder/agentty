# Agent remote-control MVP

## Terminal-automation convergence

The control path is `prompt → wait → read → send-keys`, entirely inside the
existing kitty process and PTY lifecycle. There is no independent terminal
server, detach/reattach protocol or PTY survival after kitty exits.

```sh
kitten @ agent --match id:42 inspect
kitten @ agent-prompt --target INSTANCE --wait --until idle,done,blocked \
  --timeout-ms 120000 'Review the current diff'
kitten @ agent-read --target INSTANCE --source recent-unwrapped --lines 120
kitten @ agent-control --target INSTANCE send-keys esc
```

Prompt submission requires live idle/done evidence. Working, blocked and unknown
are refused; this intentionally does not queue a second prompt into an active
turn. `--wait` first requires post-submission working/blocked activity, then a
selected settled state. A five-second activity gate reports
`agent_prompt_stalled`; total timeout takes precedence. Startup idle does not
count as completion. Results identify baseline, working and completion sequences
where observed, but do not claim provider-native turn acceptance or success.
Timeout, loss of identity, window closure or event gaps never cause automatic
prompt replay. Request-id deduplication suppresses input redispatch only; it does
not create a durable task or a provider execution acknowledgment.

`agent-read` visible/detection reads remain passive. Recent sources default to
80 rendered rows and accept 1..10000; recent-unwrapped joins soft wraps after
limiting rendered rows. When a recent request exceeds the visible alternate
screen, an idle agent can be read using bounded mouse-wheel pagination. Use
`--passive` to guarantee no mouse input. This changes the authorization meaning
of `agent-read`: permitting it without `--passive` permits temporary TUI input.

```sh
kitten @ agent-read --target INSTANCE --source recent-unwrapped --lines 120
kitten @ agent-read --target INSTANCE --source recent-unwrapped --lines 120 --passive
```

The history collector probes the bottom, merges only unambiguous overlapping
pages, and verifies restoration before returning collected text. Collection is
limited to 128 pages, 1 MiB and 15 seconds, followed by at most five seconds of
restoration. Unsupported mouse input, ambiguous pages, timeouts or unsafe identity
changes fall back to the original passive text. Inspect `active_read`,
`restoration` and `completeness.reason`; fallback does not guarantee the viewport
was restored. Cancellation attempts bounded recovery only while the same target
remains safe. Shutdown stops input immediately. Concurrent physical user input
cannot be completely excluded. No session files are read, no separate server is
started, and even a successful read is not a complete structured transcript.

The provider bridge is observation-only. Unused native cancellation dispatch,
acknowledgment state machines and broker outbound command queues have been
removed. Cancellation remains guarded terminal Ctrl+C, with unavailable stop
confirmation. Approval/question interaction remains deliberate terminal input,
not a cross-provider approval API. Startup/resume still return launch-request
confirmation; startup readiness waiting and real-provider smoke verification
remain follow-up work.

## Scope

The existing terminal controls remain compatible. This MVP adds instance-bound
inspection, an in-memory event journal and a managed OpenCode observation bridge.
It does not provide transcript/tool history, structured approval replies, process
tree termination or confirmed native OpenCode cancellation.

## Commands

```sh
kitten @ agent --match id:42 inspect
kitten @ agent-events
kitten @ agent-events --cursor '{"runtime_epoch":"EPOCH","seq":0}' --max-events 100
kitten @ agent-subscribe --cursor '{"runtime_epoch":"EPOCH","seq":10}' --timeout-ms 10000
```

`inspect` adds `schema_version`, `runtime_epoch`, `instance_id`,
`binding_revision`, capabilities, provider state and a structured `cursor`.
The old `list/get/explain/read` interfaces retain their existing shape. Their
integer tracker cursor is separate from the new epoch-scoped service cursor.

`agent-events` reads one batch. `agent-subscribe` performs one bounded long poll,
not a permanent streaming subscription. Repeat it with the returned cursor for
continuous observation. The maximum wait is 10 seconds and the maximum batch is
100 events. Omitting the cursor begins at the current head. These commands use
the existing kitty remote-control authorization policy and expose observations
across windows; authorize their command names accordingly.

The journal retains at most 256 events and 256 KiB in memory. A cursor from a
different runtime, an expired cursor or an invalid future position returns
`resync_required` with a snapshot and replacement cursor. A snapshot restores
current knowledge, not events lost in the gap. Event coverage is limited to
observed transitions after the service is created; it is not an audit log or a
complete provider conversation history.

## Guarded controls

Use the identity returned by `inspect` when sending input:

```sh
kitten @ agent-control --match id:42 --expected-agent opencode \
  --expected-instance INSTANCE --expected-epoch EPOCH --expected-binding 1 \
  --request-id request-001 cancel
```

The new guards are optional for backward compatibility. `request-id` suppresses
duplicate dispatch within the bounded in-memory request cache (256 entries).
Reusing a key with a different request is rejected. It is not durable or
exactly-once delivery. Control responses describe terminal dispatch only;
sending Ctrl+C is not confirmation that a task or process stopped.
Available process birth metadata participates in identity; absence of that
metadata cannot prove protection against an unobserved PID reuse.

## Managed OpenCode bridge

Install/update the existing OpenCode integration using the project's agent
integration installation flow, then start OpenCode through `agent-start` or
`agent-resume`. An already-running process does not acquire the bridge by being
inspected.

```sh
kitten @ agent-start --agent opencode --cwd /absolute/project --request-id start-001
```

The managed launch supplies a private local Unix socket and a per-launch token.
The broker checks registration, live window/process/session identity and event
scope. Only the main loop accesses kitty objects; socket I/O runs in a bounded
background worker. Kitty shutdown removes the timer, worker and socket.

The reporter forwards only lifecycle/status, permission/question identifiers and
child-session relationships. It does not forward prompt text, answers, error
details, credentials, transcript or tool payloads. Tokens are not retained in
the public journal. Capabilities come from the connected reporter, not merely
from the agent executable name. Disconnect revokes those capabilities; reconnect
and startup snapshots explicitly indicate partial coverage.

The current OpenCode TUI integration has no verified native cancellation/reply
API. It therefore does not advertise one. Terminal `cancel` remains Ctrl+C;
approval/question observation does not imply approval/question control.

## Verification

Run native modules with the repository's kitty launcher, not system Python:

```sh
./kitty/launcher/kitty +launch test.py --module agent_rc
./kitty/launcher/kitty +launch test.py --module agent_control_rc
./kitty/launcher/kitty +launch test.py --module agent_wait_rc
./kitty/launcher/kitty +launch test.py --module agent_cli_contract
./kitty/launcher/kitty +launch test.py --module agent_broker
./kitty/launcher/kitty +launch test.py --module test_agent_service_native_free
./kitty/launcher/kitty +launch test.py --module test_agent_broker_native_free
go test ./tools/cmd/at
```

The broker tests exercise real local sockets and strict message validation. The
reporter test executes JavaScript against a fake TUI/socket host. These are not
proof of compatibility with every installed OpenCode version; a real provider
smoke test remains a separate release check.
