AI agents sidebar
==================

kitty can monitor recognized AI coding agents running in its terminal windows and
show them in a native macOS vertical surface on the left side of the kitty
window. It is not a nested TUI and does not use kitty's existing tab-bar
renderer.

Working indicators rotate in independently composited Core Animation layers;
their motion does not redraw the row's text and icons. They stop when hidden or
no longer working, and remain static when macOS Reduce Motion is enabled.

On macOS, press :kbd:`Cmd+B` to show the sidebar and focus its agent list.
Press it again, or :kbd:`Esc`, to return to the terminal without hiding the
sidebar. :kbd:`Cmd+Shift+B` shows or hides the sidebar. Both shortcuts can be
rebound in :file:`kitty.conf`::

    map cmd+b focus_agents_sidebar
    map cmd+shift+b toggle_agents_sidebar

Keyboard navigation
-------------------

The sidebar has its own native keyboard focus. Up and Down move the selection
through group headings and expanded agents without switching the active terminal
or marking an agent's completion as read. Keyboard selection uses the same subtle
filled highlight as mouse hover, without a border. The current terminal retains
its distinct filled active highlight. Hovering an agent or group shows a pointing
hand cursor; hovering the resize rail shows a horizontal resize cursor.
Press Enter on an agent to activate its terminal and return keyboard input to it;
Enter on a group expands or collapses it. Escape returns without activating the
selection.

Left collapses a group, or moves an agent selection to its parent group. Right
expands a group, or enters its first agent. Home and End select the first and last
items; Page Up and Page Down move by a viewport. The list scrolls immediately to
keep the selection visible. On compact Mac keyboards, use the corresponding
Fn-modified navigation keys.

Press Shift+F10 in the list, or right-click, to open the actions menu. Tab,
Shift+Tab and Escape return to the terminal; there is no footer button.
Navigate the menu with arrow keys and Enter; Escape dismisses the menu first.
The menu offers agent activation, a new terminal in the selected agent's working
directory, copying that directory, and closing the agent's window using the
existing close-confirmation rules. It also offers sidebar width adjustments in
two-column steps, expanding or collapsing all groups, and hiding the sidebar.
There is no Delete-to-close shortcut.

Selection is retained by agent and group identity when titles or statuses change.
If a selected agent disappears, its pending action is not redirected to another
agent. Closing or hiding the sidebar returns focus before removing its controls.
Switching away from its native window ends navigation without stealing focus
back. A cancelled close confirmation restores sidebar focus only if the user has
not moved elsewhere. An empty list still provides sidebar view actions.

Click an agent row to activate its terminal, or click a group heading to expand
or collapse it. Mouse operation remains available alongside keyboard navigation.

Sidebar appearance and scope
----------------------------

The sidebar works with either value of :opt:`macos_native_tabs`. With
:code:`macos_native_tabs no`, kitty's tab bar remains visible beside the
sidebar, at its configured :opt:`tab_bar_edge`, so tabs without agents remain
accessible. Its normal visibility settings, including :code:`tab_bar_style
hidden` and :opt:`tab_bar_min_tabs`, still apply. With native tabs enabled,
macOS continues to provide the tab bar.

Right-click (or control-click) an agent row for a context menu acting on that
row's agent window: focus it, open a new window in its working directory, copy
that directory to the clipboard, or close the window. The menu is bound to the
agent window, not the row position, so a sidebar refresh while the menu is open
cannot redirect an action to a different agent. Empty lists and group headings
offer only sidebar view actions, not agent actions.

The macOS surface uses a translucent sidebar material, grouped sections,
two-line agent rows, colored state indicators, and active-row highlighting.
A working agent shows a turning arc in place of the state dot; the animation
only runs while something is actually working. Drag the sidebar's visible
right-edge rail to change its width; the sidebar always spans the full window
height. The sidebar toggle is a native titlebar accessory after the traffic-light
controls; AppKit reserves its width so the window title starts to its right.
Its expanded and collapsed states use different background and chevron styles.

The Agents section heading displays the active grouping, agent count, and group
count, for example ``Grouping: tab · 3 agents · 2 groups``. Click the heading to
collapse or expand the section. The grouping strategy is configurable with
:opt:`agents_sidebar_grouping`:

* ``time`` (default): group by Today, Yesterday, and Earlier;
* ``agent``: group rows by agent name;
* ``tab``: group rows by tab sequence number and kitty tab title;
* ``path``: group rows by the full working directory.

Group headings use a full-width shaded band and a theme-aware disclosure chevron.
Their entire clickable area uses the pointing-hand cursor, including the chevron.
When the sidebar has keyboard focus, a full accent border surrounds the sidebar
until focus returns to the terminal. The border is a focus indicator, separate
from the active-agent highlight.
Expanding or collapsing a group uses a short 180 ms native transition for its
children and following groups. Mouse and keyboard operations share the same
transition. macOS Reduce Motion disables it; status/title refreshes do not
restart the animation.
The scrollable native outline exposes group hierarchy, expansion, agent labels,
status and the current-terminal marker to accessibility clients. Collapse state
is saved with a persisted kitty session, together with sidebar visibility and
width. Transient keyboard focus and selection are not restored at startup.

Visible sidebars refresh agent state every second, including after session
restoration and for unfocused panes. Lifecycle reports and other agent events
can trigger an earlier update; switching focus is not required.

Terminal editors
~~~~~~~~~~~~~~~~

On macOS, the sidebar has separate **AI Agents** and **Editors** sections.
Full-width section headings distinguish the two areas, with a disclosure
indicator on the right showing whether each section is expanded or collapsed.
Each section's content shares its heading's background tint; hover emphasis
applies only to the heading, while active rows retain their selection highlight.
Click a section heading to collapse or expand it, and drag the horizontal
divider to change their height allocation. Each section scrolls independently;
collapsing one gives the other its available space.
Section collapse states and the preferred height ratio are saved as native
application preferences, independently of the session's agent-group collapse
state. Available window space constrains the displayed heights without
discarding the preferred ratio.

The Editors section automatically recognizes local foreground ``nvim``, ``vim``,
``vi``, ``view``, ``nano``, ``micro``, ``hx``/``helix``, ``kak`` and terminal
Emacs (``emacs -nw`` or ``emacsclient -t``), plus the supported Vim diff entry
points. No editor plugin, configuration change or RPC server is required.
Click an editor to focus its kitty pane and the containing tab or window.
Entries show the editor's brand icon together with the terminal title and
working directory, not an authoritative current buffer or unsaved-file status.
Editors do not participate in agent lifecycle states or completion notifications.

Agent and editor rows in the same project are linked. Selecting either row
highlights matching rows in the other section with a subtle background, left
rail and link badge. Matching uses the same Git worktree root, or the exact
working directory outside Git. Selection is exclusive across the two sections,
so clicking a row in either section moves the link source and clears the other
selection. Hovering a row does not activate the link state.

Editor discovery shares the visible sidebar's existing refresh cycle and process
cache; it does not scan terminal screen contents or create a separate polling
timer. An editor disappears when it exits or leaves the foreground, including
when suspended with :kbd:`Ctrl+Z`. Headless, embedded and common noninteractive
invocations are excluded. Editors inside tmux, SSH or nested terminals are not
guaranteed to be discoverable; this is pane-level navigation, not tmux pane or
editor buffer navigation.

The sidebar is opened and closed manually with the persistent floating
sidebar button at the upper-left of the window or the
:code:`toggle_agents_sidebar` action; it is not opened automatically when an
agent starts.


The sidebar visibility is global for the kitty process. Toggling it in any tab
opens or closes it in every existing and newly created tab; the Agent rows remain
scoped to the current native tab group or OS window.


Agents are recognized by their foreground process. kitty ships detection rules
for Claude Code, Codex, OpenCode, Gemini, GitHub Copilot, Cursor Agent, Grok,
Qwen, Kimi, Kiro, Kilo, Cline, Droid, Devin, Amp, Muse, Maki, Letta, Hermes,
Antigravity, Qoder and pi. A process is shown only while it is the foreground
process of a kitty window; unsupported programs remain ordinary terminal
windows and are not shown.

How a state is decided
-------------------------

The foreground process answers *which* agent is in the window. It does not
answer *what the agent is doing*. That comes from one of two sources:

* For agents whose integration reports every lifecycle transition, their own
  OSC 7501 program-status records.
* For every other agent, the live contents of the window.

The distinction matters. An integration that only reports session identity has
no handler for cancelled turns and interrupts, so its last report goes stale
and would otherwise pin the window to a state its own screen contradicts.

Screen detection is rule-based. Each agent has a table of rules in
:file:`kitty/agent_manifests/`, and each rule matches inside one named region
of the window: the title, the OSC 9;4 progress payload, the bottom few
non-empty lines, the prompt box interior, the text below the last horizontal
rule, and so on. Every rule is evaluated and the highest-priority match wins.

Two properties follow from this design and are worth knowing:

* ``working`` requires a visible marker, such as a spinner, an interrupt hint,
  a progress bar or a live timer. Output scrolling past is not treated as work,
  because an agent that has finished its turn can still have background tasks
  writing to the window.
* Screens that replace the live interface, such as a transcript viewer or a
  model picker, hold the previous state rather than reporting a new one. They
  say nothing about what the agent is doing, and guessing from them would flip
  a working agent to idle while the user is only reading back.

The displayed states are ``working``, ``blocked``, ``done``, ``idle`` and
``unknown``. ``done`` only ever comes from a program-status record, since
completion is not something the screen can show reliably. ``unknown`` means the
process is a recognized agent but kitty ships no rules for it, so its activity
genuinely cannot be read; it is reported rather than guessing ``idle``.

When a screen-detected agent finishes while its window is not focused, kitty
keeps ``done`` instead of immediately returning to ``idle``. Viewing the
window clears it to ``idle``. Kitty also emits a desktop notification for the
transition; clicking that notification focuses the corresponding agent
window.

The sidebar is scoped to the current kitty tab group. With macOS native tabs,
all tabs in the native tab group share one Agents list and each tab displays the
same sidebar. Without native tabs, it covers all kitty tabs in the current OS
window. It does not keep agent processes alive after kitty exits.

.. _agent_resume:

Resuming agent conversations after a restart
-----------------------------------------------

When kitty restores a session saved by :opt:`persist_session`, it can restart
an agent on the conversation it was in, rather than leaving you with a shell.
To enable this, set in :file:`kitty.conf`::

    resume_agents_on_restore yes

This is off by default, because resuming an agent starts it running again,
which can consume API credits.

Before resuming an agent, kitty starts the shell configured by :opt:`shell`
as an interactive login shell, so its startup files can set PATH, API keys and
other environment variables. The resume command runs after that initialization,
in the saved working directory, even when shell integration is disabled. The
window stays open when the agent exits so its output remains readable.
Restored scrollback is followed by two blank lines, the ``Restored from ...``
timestamp and a divider marking where the restored history ends. Two blank
lines below the divider separate it from new output.

An agent's conversation lives in the agent's own storage, not in kitty, so
kitty needs two things to resume one: the conversation's id, and the command
that reopens it. The command comes from the agent's manifest, which ships with
kitty, so a saved session can never make kitty run a command of its own
choosing. Only the id comes from the agent, and an id that does not look like
an id is discarded rather than used.

The id reaches kitty from a hook or native plugin that runs inside the agent.
Install the reporter for each agent you use::

    kitty +agent-integration install claude
    kitty +agent-integration install codex
    kitty +agent-integration install omp
    kitty +agent-integration install pi
    kitty +agent-integration install opencode

Check or remove it with::

    kitty +agent-integration status
    kitty +agent-integration uninstall omp

Claude Code and Codex use a ``SessionStart`` hook. OMP and Pi use a native
extension, and OpenCode uses a TUI plugin that follows the conversation selected
in that terminal. Installing leaves other hooks and plugins untouched. Restart
the agent after installing its reporter; enabling ``resume_agents_on_restore``
alone does not install a reporter or recover an identity missing from an older
snapshot.

First-launch integration prompts
---------------------------------

Kitty checks supported local agents when their window has focus, even when the
Agents sidebar is hidden. If a Kitty hook or reporter is missing or outdated,
it offers ``Install`` or ``Update`` and lists the files that may change.
Nothing is installed without consent, no input is sent to the agent, and the
agent is never restarted automatically. Existing hooks and plugins are kept;
custom files that conflict with Kitty's reporter are not overwritten.

``Later`` defers the prompt for that agent/configuration for the current kitty
run. ``Never ask again`` remembers the dismissal per agent across restarts.
Manual installation with ``kitty +agent-integration install <agent>`` remains
available. Installation checks compare generated file contents and plugin/hook
registration; a file merely existing does not mean it is current.

SSH windows and agents whose configuration environment differs from Kitty's
are not offered local installation. Restart the agent after accepting an
installation so it loads the new hook or plugin; kitty does not hot-reload an
already-running agent.

If an agent rewrites its process title and its environment can no longer be
read, the check falls back to Kitty's recorded child environment. Explicitly
different configuration paths still prevent local installation.

The hook reports the id by setting the ``KITTY_AGENT_SESSION`` user variable
with an OSC 1337 escape code. It writes to the
device named by the ``KITTY_WINDOW_TTY`` environment variable, which kitty sets
for every program it runs, rather than to :file:`/dev/tty`: Claude Code runs
its hooks without a controlling terminal, so :file:`/dev/tty` is not available
to them.

Any agent can report itself the same way, without a hook kitty ships, by
setting those user vars itself::

    printf '\033]1337;SetUserVar=KITTY_AGENT_SESSION=%s\007' "$(printf %s "$ID" | base64)"

Resuming works for any agent whose official integration or native adapter
reports a session reference. Agents without a kitty reporter are restored as
ordinary shell windows in their saved working directory, with their scrollback.

Herdr-native resume coverage
-----------------------------

kitty ships resume commands for the same native-resume agent set documented
by Herdr:

* ``pi --session <id>``
* ``omp --resume=<id>``
* ``claude --resume <id>``
* ``codex resume <id>``
* ``copilot --resume=<id>``
* ``devin --resume <id>``
* ``droid --resume <id>``
* ``kimi --session <id>``
* ``opencode --session <id>``
* ``kilo --session <id>``
* ``hermes --resume <id>``
* ``qodercli --resume <id>``
* ``qwen --resume <id>``
* ``letta --conversation <id>``
* ``cursor-agent --resume <id>``
* ``mastracode --thread <id>``
* ``agy --conversation <id>``
* ``grok --resume <id>``

The command alone is not enough to restore a conversation. The agent must
report its session reference to kitty. Install kitty's reporters for Claude
Code, Codex, Pi, OMP and OpenCode using the commands above. Other agents remain
``plugin-required`` or ``adapter-required`` until a native kitty reporter is
available. Herdr plugins that only send to Herdr's socket do not report to kitty.

For Pi and OMP, kitty accepts either a session id or an absolute session path
through ``KITTY_AGENT_SESSION`` and ``KITTY_AGENT_SESSION_PATH``. Paths are only
accepted for those two agents; when both are present, the absolute path takes
precedence. The reporter updates the reference when the agent changes sessions,
so two panes using the same working directory do not share a guessed identity.

kitty does not speak Herdr's server socket protocol. It preserves the same
source/agent/session-reference semantics but transports the reference through
the kitty window's OSC 1337 user variables.

Native agent remote control
---------------------------

Use ``kitten @ agent`` to query agents through kitty's native remote-control
interface::

    kitten @ agent list
    kitten @ agent --match id:42 explain
    kitten @ agent --match id:42 --extent detection read

These read-only commands list recognized agents, explain detection and state,
or read the live detection screen of the selected kitty window. They reuse
kitty remote-control transport, connection selection and authentication;
configure access as for any other ``kitten @`` command. No separate herdr
wrapper is required.

Guarded agent control
~~~~~~~~~~~~~~~~~~~~~

``agent-control`` is a separate, mutating remote command::

    kitten @ agent-control --match id:42 --expected-agent claude prompt 'Review this change'
    kitten @ agent-control --match id:42 --expected-agent claude --expected-session SESSION send-keys ctrl+l
    kitten @ agent-control --match id:42 --expected-agent claude focus
    kitten @ agent-control --match id:42 --expected-agent claude cancel

Every action requires an exact positive ``id:N`` selector and a matching live
foreground agent type. There is no active-window default, pattern matching or
broadcast. Optional ``--expected-session`` checks the current live session
reference, not a cached tracker record. Window ids alone cannot distinguish
same-type restarts; use the session guard where available. These guards are not
an atomic lock on the process or a substitute for remote-control authentication.

``prompt`` takes exactly one non-empty argument, sent as literal UTF-8 (backslash
escapes are not interpreted), followed by an Enter key. It requires live state
arbitration to report ``idle``; working, blocked, unknown and live done states
are rejected. A sidebar done hold does not prevent a live idle prompt. ESC and
terminal control characters are forbidden, except newline and tab. Those two
require the target's current bracketed-paste mode; when enabled, kitty uses
sanitized bracketed paste. No option forces paste mode or bypasses the idle guard.
All arguments and guards are checked before any input is written.

``send-keys`` accepts the same key syntax as ``send-key`` (native key codes are
not allowed); it deliberately bypasses the prompt idle guard. ``focus`` requests
window activation, including switching OS windows. ``cancel`` only sends
``ctrl+c``: it never signals or kills a process. JSON results explicitly report
``input_requested``, ``focus_requested`` and ``cancel_requested``; they do not assert
that a prompt completed, focus was acquired, or an agent stopped. Input delivery
can still fail or be ignored by the application. These commands do not refresh
the tracker or sidebar except that ``prompt`` observes the target before sending
input to establish its event baseline.

One-shot agent waits
~~~~~~~~~~~~~~~~~~~~

``agent-wait`` returns one JSON event batch, not a subscription. Each call requires
``--match id:N`` (a positive exact window id) and ``--expected-agent NAME``.
``--expected-session`` optionally guards the live session identity. Its options are:

* ``--after-seq``: a nonnegative event cursor; omitted (native default ``-1``)
  starts at the current global cursor, not at the beginning of retained history.
  A cursor ahead of the current cursor is rejected.
* ``--until``: ``changed`` (default), or comma-separated states from ``idle``,
  ``working``, ``blocked``, ``done`` and ``unknown``.
* ``--require-working``: off by default. For state-filtered waits, require an
  observed ``working`` event after the cursor in the same generation before a
  matching state event. It does not change ``--until changed`` behavior.
* ``--timeout-ms``: integer 0..10000, default 0 (return immediately).
* ``--max-events``: integer 1..100, default 100.

After a successful ``prompt`` request, take ``baseline_seq`` from its JSON result
and use it as ``CURSOR`` below (do not omit the cursor)::

    kitten @ agent-wait --match id:42 --expected-agent claude --expected-session SESSION --after-seq CURSOR --until done,blocked --require-working --timeout-ms 10000

The baseline is captured after observing the target and before writing the
prompt. This avoids treating an old done hold as completion of the new prompt.
It is not a prompt id or proof that input was accepted. Only observed events
count: a fast turn with no observed working transition may time out. ``done``
means an observed completion state, not success; ``blocked`` may need user input.
Neither state proves the requested task was completed correctly.

Events have a global ``seq``, ``window_id`` and a ``snapshot`` (possibly null).
For pagination use ``next_seq``, not ``head_seq``: the latter is the global head
and can skip unconsumed target events. Continue while ``has_more`` is true.
State-filtered waits consume nonmatching pages internally; inspect ``reason``
rather than assuming every returned event matches the requested state.
``timeout`` is not task failure. Start another one-shot call with its returned
``next_seq`` to continue; the require-working observation is local to each call,
not retained across calls. A client needing a working latch across timeouts should
consume ``changed`` batches and maintain that latch itself.

The journal retains only 256 events across all windows. ``reason: truncated``
with ``resync_required: true`` means history was lost. Reconcile the returned
``snapshot`` with a fresh agent query and restart from ``resync_cursor``; do not
infer completion from the missing interval. ``window_closed`` and
``identity_changed`` terminate the wait and require rediscovery, not blind retry.
Snapshots carry ``generation`` to distinguish observed identity changes within
a window. State matching is restricted to the generation captured at wait entry;
window ids, session ids and cursors are not interchangeable lifecycle identities.

To combine launch and control, first use ``agent-start`` or ``agent-resume`` and
retain its returned window id. Both report ``identity_confirmed: false``: query
that exact window until the expected live agent (and session, when available)
is established before sending guarded input. Then use the prompt baseline and
wait loop above. Launch acceptance is neither readiness nor conversation identity
confirmation, and a wait is not a launch identity-confirmation handshake.

Controlled agent launch
~~~~~~~~~~~~~~~~~~~~~~~~

``agent-resume`` creates a new window using only ``resume_argv_for`` and the
manifest command. Explicit mode requires ``--agent NAME --session REF --cwd``;
source mode requires exact ``--match id:N --expected-agent NAME`` and may use
``--expected-session``. The modes are mutually exclusive, session references
are validated, and an already-active matching session is rejected. The result
reports ``launch_requested`` and the new window id, not identity confirmation.
Explicit mode uses the current active tab; source mode uses the source tab
and live cwd. Source mode deliberately excludes its own source from duplicate
checks, allowing a requested new-window resume of that conversation; any
other live matching window is rejected. Neither mode stops the source.
Recent resume launches are also rejected for up to 60 seconds to cover the
interval before foreground identity reporting catches up.

``agent-start`` creates a new window from a small trusted registry (currently
``claude``, ``codex``, ``opencode``, ``pi`` and ``omp``). It requires an
existing absolute ``--cwd`` and ``--request-id``. Request ids are idempotent
for a bounded 60-second/256-entry in-memory window; only successful launches
are cached. Neither command accepts arbitrary executable, argv, watcher,
stdin, environment or privilege options. A failed launch is not retried and
does not close or kill any target.
Idempotency records belong to the running kitty process and disappear on
restart, expiry or oldest-entry eviction. Reusing a retained request id with
different parameters is rejected; replay returns the original window id,
even if that window has since exited. Requests are serialized while launching.
A thrown launch failure is not cached and may have partial side effects:
inspect existing windows before retrying. ``identity_confirmed: false`` means
launch acceptance alone does not establish that the new foreground process
is the requested agent or that it opened the requested conversation.
