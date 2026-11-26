#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

# Install the hooks that let AI coding agents tell kitty which conversation they are in, so that kitty can resume that
# conversation when it restores a saved session, see resume_agents_on_restore.
#
# An agent reports its session id by setting a kitty user var over OSC 1337. The hook that does it runs as a subprocess of the
# agent, and Claude Code runs its hooks without a controlling terminal, so the hook cannot use /dev/tty. It writes to
# KITTY_WINDOW_TTY instead, the device of the kitty window the agent runs in, which kitty puts in the environment of every child.
# Writing to it needs no remote control, no socket and no cooperation from the agent beyond running the hook.

import json
import os
import re
import shutil
from collections.abc import Callable, Sequence
from typing import Any, NamedTuple

from .agents import AGENT_ID_VAR, AGENT_SESSION_PATH_VAR, AGENT_SESSION_VAR, AGENT_SOURCE_VAR
from .constants import config_dir

HOOK_DIR_NAME = 'agent-integration'
KITTY_ASSET_MARKER = 'KITTY_AGENT_INTEGRATION_ID=kitty'

# These reporters deliberately use only the current session supplied by the host
# APIs. They never inspect session databases or infer a session from cwd.
PI_REPORTER = r"""// KITTY_AGENT_INTEGRATION_ID=kitty
import fs from "node:fs";
import path from "node:path";

const AGENT = "%(agent)s";
const SOURCE = `kitty:${AGENT}`;
const keys = ["%(id_var)s", "%(source_var)s", "%(session_var)s", "%(path_var)s"];
function absolute(value) {
  return typeof value === "string" && (path.posix.isAbsolute(value) || path.win32.isAbsolute(value)) ? value : undefined;
}
function emit(id, sessionPath) {
  const tty = process.env.KITTY_WINDOW_TTY;
  if (!tty) return;
  const values = { "%(id_var)s": AGENT, "%(source_var)s": SOURCE, "%(session_var)s": id, "%(path_var)s": sessionPath };
  const out = keys.map((key) => {
    const value = values[key];
    return value ? `\x1b]1337;SetUserVar=${key}=${Buffer.from(value).toString("base64")}\x07` : `\x1b]1337;SetUserVar=${key}\x07`;
  }).join("");
  try { fs.writeFileSync(tty, out); } catch {}
}
function reportSession(ctx) {
  if (ctx?.hasUI !== true) return;
  let sessionPath;
  let id;
  try { sessionPath = absolute(ctx?.sessionManager?.getSessionFile?.()); } catch {}
  try { const value = ctx?.sessionManager?.getSessionId?.(); id = typeof value === "string" && value ? value : undefined; } catch {}
  // A path is authoritative when available; otherwise use the exact API id.
  emit(id, sessionPath);
}
export default function (pi) {
  const clear = (_event, ctx) => { if (ctx?.hasUI === true) emit(); };
  const report = (_event, ctx) => reportSession(ctx);
  pi.on("session_before_switch", clear);
  pi.on("session_before_fork", clear);
  pi.on("session_start", report);
  pi.on("session_switch", report);
  pi.on("session_shutdown", clear);
}
"""

# The optional structured provider lane uses agent_provider.py's field contract.
# It requires both explicit broker environment variables; OSC remains independent.
OPENCODE_REPORTER = r"""// KITTY_AGENT_INTEGRATION_ID=kitty
import fs from "node:fs";
import net from "node:net";
const AGENT = "opencode";
const SOURCE = "kitty:opencode";
const ID_KEY = "%(id_var)s";
const SOURCE_KEY = "%(source_var)s";
const SESSION_KEY = "%(session_var)s";
const tty = process.env.KITTY_WINDOW_TTY;
let currentSession;
const lastStates = new Map();
const providerStates = new Map();
let writeChain = Promise.resolve();
let broker;
let brokerSequence = 0;
let brokerAPI;
let brokerTimer;
let brokerAttempts = 0;
let brokerDisposed = false;
let brokerGap = true;
const childParents = new Map();
const brokerCapabilities = ["session_lifecycle", "session_status", "parent_session", "interaction_id"];

function brokerWrite(value) {
  const socket = broker;
  if (!socket || socket.connecting || socket.destroyed) { brokerGap = true; return; }
  if (socket.writableLength > 65536) { brokerGap = true; socket.destroy(); return; }
  try { socket.write(JSON.stringify(value) + "\n"); }
  catch { brokerGap = true; socket.destroy(); }
}

function providerMarker(kind, payload) {
  brokerWrite({ type: "event", event: { kind, session_id: currentSession,
    payload, event_id: String(++brokerSequence) } });
}

function scheduleBroker() {
  if (brokerDisposed || brokerTimer || !currentSession || brokerAttempts >= 6) return;
  const delay = Math.min(100 * 2 ** brokerAttempts++, 3200);
  brokerTimer = setTimeout(() => { brokerTimer = undefined; connectBroker(); }, delay);
  brokerTimer.unref?.();
}

function connectBroker() {
  const socketPath = process.env.KITTY_AGENT_BROKER_SOCKET;
  const token = process.env.KITTY_AGENT_BROKER_TOKEN;
  // Explicit local Unix socket opt-in only, with no discovery or daemon startup.
  if (!socketPath || !socketPath.startsWith("/") || !token || !currentSession || brokerDisposed) return;
  try {
    const socket = net.createConnection({ path: socketPath });
    broker = socket;
    socket.on("error", () => { socket.destroy(); });
    socket.on("close", () => {
      if (broker !== socket || brokerDisposed) return;
      broker = undefined;
      brokerGap = true;
      scheduleBroker();
    });
    socket.on("connect", () => {
      if (broker !== socket || brokerDisposed) { socket.destroy(); return; }
      brokerWrite({ type: "register", protocol_version: 1,
        token, agent: AGENT, session_id: currentSession, capabilities: brokerCapabilities });
      if (brokerGap) {
        brokerGap = false;
        providerMarker("gap", { reason: "startup_or_disconnected", partial: true, children: "unknown" });
      }
      // Reintroduce known relationships in insertion (parent-first) order.
      for (const [id, parentID] of childParents) reportProviderEvent({ type: "session.created", properties: { info: { id, parentID } } });
      let status;
      try { status = providerStatus(brokerAPI, currentSession); } catch {}
      if (["busy", "retry", "idle"].includes(status)) reportProviderEvent({ type: "session.status", properties: { sessionID: currentSession, status } }, true);
    });
    // No verified TUI cancellation API: do not advertise turn.cancel, execute
    // commands or acknowledge success. Discard inbound bytes without buffering.
    socket.on("data", () => {});
    socket.unref();
  } catch { broker = undefined; brokerGap = true; scheduleBroker(); }
}

function reportProviderEvent(event, snapshot = false) {
  const p = event?.properties;
  if (!p || typeof p !== "object") return;
  const kind = event.type;
  const sessionID = p.sessionID || ((kind === "session.created" || kind === "session.deleted") ? p.info?.id : undefined);
  if (typeof sessionID !== "string" || !sessionID) return;
  const value = { kind, session_id: sessionID, payload: {}, event_id: String(++brokerSequence) };
  if (snapshot) Object.assign(value.payload, { snapshot: true, partial: true, children: "unknown" });
  if (typeof p.info?.parentID === "string" && p.info.parentID) value.parent_session_id = p.info.parentID;
  if (kind === "session.status") {
    const status = statusOf(p.status);
    if (!["busy", "retry", "idle"].includes(status)) return;
    value.payload.status = status;
  }
  if (kind.startsWith("permission.") || kind.startsWith("question.")) {
    const id = kind.endsWith(".asked") ? p.id : p.requestID;
    if (typeof id === "string" && id) value.interaction_id = id;
  }
  brokerWrite({ type: "event", event: value });
}

function write(value) {
  if (!tty) return;
  writeChain = writeChain.then(() => {
    try { fs.writeFileSync(tty, value); } catch {}
  });
}

function userVar(key, value) {
  return value === undefined
    ? `\x1b]1337;SetUserVar=${key}\x07`
    : `\x1b]1337;SetUserVar=${key}=${Buffer.from(value).toString("base64")}\x07`;
}

function reportIdentity(sessionID) {
  write(userVar(ID_KEY, AGENT) + userVar(SOURCE_KEY, SOURCE) + userVar(SESSION_KEY, sessionID));
}

function reportStatus(sessionID, state, kind) {
  if (!sessionID || !tty) return;
  const recordID = `${sessionID}/kitty`;
  const signature = `${state}:${kind || ""}`;
  if (lastStates.get(recordID) === signature) return;
  lastStates.set(recordID, signature);
  const fields = [`id=${recordID}`, `state=${state}`, "app=opencode"];
  if (kind) fields.splice(2, 0, `kind=${kind}`);
  write(`\x1b]7501;${fields.join(":")}\x07`);
}

function clearStatus(sessionID = currentSession) {
  if (!sessionID) return;
  const recordID = `${sessionID}/kitty`;
  write(`\x1b]7501;state=clear:id=${recordID}\x07`);
  lastStates.delete(recordID);
}

function routeSession(api) {
  const route = api.ui.router.current();
  return route.type === "session" && route.sessionID !== "dummy"
    ? api.data.session.root(route.sessionID) : undefined;
}

function statusOf(value) {
  return typeof value === "string" ? value : value?.type;
}

function providerStatus(api, sessionID) {
  return api.data.session.status(sessionID) === "running"
    || api.data.session.pending.list(sessionID).some(item => item.type !== "synthetic") ? "busy" : "idle";
}

function sessionState(api, sessionID) {
  if (api.data.session.permission.list(sessionID)?.length) return ["blocked", "permission"];
  if (api.data.session.form.list(sessionID)?.length) return ["blocked", "question"];
  if (providerStatus(api, sessionID) === "busy") return ["working"];
  return [api.data.session.get(sessionID)?.outcome === "failed" ? "error" : "idle"];
}

function isTrackedSession(sessionID) {
  return sessionID === currentSession || childParents.has(sessionID);
}

function sync(api) {
  const sessionID = routeSession(api);
  if (sessionID !== currentSession) {
    clearStatus(currentSession);
    childParents.clear();
    providerStates.clear();
    currentSession = sessionID;
    const previousBroker = broker;
    broker = undefined;
    previousBroker?.destroy();
    clearTimeout(brokerTimer);
    brokerTimer = undefined;
    brokerAttempts = 0;
    brokerGap = true;
    lastStates.clear();
    reportIdentity(sessionID);
    connectBroker();
  }
  if (!sessionID) return;
  // The host cache includes children already running when the plugin loads.
  const family = new Set([sessionID, ...api.data.session.family(sessionID)]);
  for (const id of childParents.keys()) {
    if (!family.has(id)) {
      reportProviderEvent({ type: "session.deleted", properties: { sessionID: id } });
      childParents.delete(id);
      providerStates.delete(id);
    }
  }
  for (const id of family) {
    if (id === sessionID) continue;
    const parentID = api.data.session.get(id)?.parentID;
    if (parentID && family.has(parentID) && childParents.get(id) !== parentID) {
      childParents.set(id, parentID);
      reportProviderEvent({ type: "session.created", properties: { info: { id, parentID } } });
    }
  }
  let state = "idle", kind;
  const priority = { idle: 0, error: 1, working: 2, blocked: 3 };
  for (const id of family) {
    const status = providerStatus(api, id);
    if (providerStates.get(id) !== status) {
      providerStates.set(id, status);
      reportProviderEvent({ type: "session.status", properties: { sessionID: id, status } });
    }
    const [next, nextKind] = sessionState(api, id);
    if (priority[next] > priority[state]) { state = next; kind = nextKind; }
  }
  // One session-scoped aggregate avoids native shared-tab summaries and stale
  // completed child records overriding work still running elsewhere in the family.
  reportStatus(sessionID, state, kind);
}

export default { id: "kitty.opencode.session", setup(api) {
  if (!tty) return;
  brokerAPI = api;
  brokerDisposed = false;
  reportIdentity(undefined);
  const off = api.data.listen(({ details: event }) => {
    sync(api);
    const data = event.data;
    const sessionID = data?.sessionID || data?.form?.sessionID;
    if (!sessionID || !isTrackedSession(sessionID)) return;
    const types = { "form.created": "question.asked", "form.replied": "question.replied", "form.cancelled": "question.rejected",
      "session.execution.failed": "session.error", "session.execution.succeeded": "session.idle",
      "session.execution.interrupted": "session.idle" };
    const type = types[event.type] || event.type;
    if (event.type === "session.created") {
      reportProviderEvent({ type, properties: { info: { id: sessionID, parentID: data.parentID } } });
    } else if (["permission.asked", "permission.replied", "question.asked", "question.replied", "question.rejected", "session.error", "session.idle", "session.deleted"].includes(type)) {
      const id = data.form?.id || data.id;
      reportProviderEvent({ type, properties: { sessionID, id, requestID: data.requestID || id } });
    }
  });
  sync(api);
  const timer = setInterval(() => sync(api), 100);
  return () => {
    brokerDisposed = true;
    clearTimeout(brokerTimer);
    clearInterval(timer);
    off();
    broker?.destroy();
    clearStatus();
    reportIdentity(undefined);
    currentSession = undefined;
    broker = undefined;
    brokerTimer = undefined;
    childParents.clear();
    providerStates.clear();
    return writeChain;
  };
}};
"""
# Written by the hook, read by kitty/window.py's OSC 1337 handler
HOOK_SCRIPT = r"""#!/bin/sh
# Installed by kitty, see kitty/agent_integration.py. Tells kitty which agent conversation this window is in, so that kitty can
# resume it after a restart. Does nothing outside kitty and never fails in a way the agent can see.
set -u
tty_path="${KITTY_WINDOW_TTY:-}"
[ -n "$tty_path" ] || exit 0
[ -w "$tty_path" ] || exit 0
session_id="${%(id_env)s:-}"
if [ -z "$session_id" ]; then
    # The id is on stdin as JSON when the agent does not export it
    session_id=$(sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)
fi
[ -n "$session_id" ] || exit 0
encoded=$(printf %%s "$session_id" | base64 | tr -d '\n')
printf '\033]1337;SetUserVar=%(session_var)s=%%s\007' "$encoded" > "$tty_path" 2>/dev/null || exit 0
printf '\033]1337;SetUserVar=%(agent_var)s=%%s\007' "$(printf %%s '%(agent)s' | base64 | tr -d '\n')" > "$tty_path" 2>/dev/null || exit 0
printf '\033]1337;SetUserVar=%(source_var)s=%%s\007' "$(printf %%s 'kitty:%(agent)s' | base64 | tr -d '\n')" > "$tty_path" 2>/dev/null || exit 0
exit 0
"""


class Agent(NamedTuple):
    # The manifest id, which is also what a saved session records
    id: str
    name: str
    # The environment variable the agent sets to the session id in hook subprocesses, empty if it sets none
    id_env: str
    # Where the agent keeps the configuration its hooks are registered in
    config_path: Callable[[], str]
    # Official integration mechanism. Plugin-only agents cannot use kitty's generic shell hook safely.
    integration_kind: str = 'hook'
    hook_event: str = 'SessionStart'
    # Environment variable and Kitty-side default that select the config home, if any
    config_env: tuple[str, str] | None = None


class IntegrationInspection(NamedTuple):
    state: str
    paths: tuple[str, ...]
    detail: str


HOOK_OWNERSHIP_MARKER = '# Installed by kitty, see kitty/agent_integration.py.'


def hook_text(agent: Agent) -> str:
    return HOOK_SCRIPT % {
        'agent': agent.id,
        'id_env': agent.id_env or 'KITTY_UNSET_SESSION_ID',
        'session_var': AGENT_SESSION_VAR,
        'agent_var': AGENT_ID_VAR,
        'source_var': AGENT_SOURCE_VAR,
    }


def owned_hook(path: str) -> bool:
    try:
        with open(path) as f:
            return HOOK_OWNERSHIP_MARKER in f.read(256)
    except OSError:
        return False


def _inspection_error(paths: tuple[str, ...], err: BaseException) -> IntegrationInspection:
    return IntegrationInspection('error', paths, str(err))


def hook_dir() -> str:
    return os.path.join(config_dir, HOOK_DIR_NAME)


def hook_path(agent: str) -> str:
    return os.path.join(hook_dir(), f'{agent}-session.sh')


def reporter_path(agent: Agent) -> str:
    if agent.integration_kind == 'native':
        return os.path.join(os.path.dirname(agent.config_path()), 'kitty-agent-state.ts')
    return os.path.join(os.path.dirname(agent.config_path()), 'kitty-agent-state', 'tui.js')


def reporter_text(agent: Agent) -> str:
    values = {
        'agent': agent.id,
        'id_var': AGENT_ID_VAR,
        'source_var': AGENT_SOURCE_VAR,
        'session_var': AGENT_SESSION_VAR,
        'path_var': AGENT_SESSION_PATH_VAR,
    }
    return (PI_REPORTER if agent.integration_kind == 'native' else OPENCODE_REPORTER) % values


def write_reporter(agent: Agent) -> str:
    path = reporter_path(agent)
    if os.path.lexists(path) and not owned_reporter(path):
        raise SystemExit(f'Refusing to overwrite an unowned reporter: {path}')
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(path, 'w') as f:
        f.write(reporter_text(agent))
    os.chmod(path, 0o700)
    return path


def owned_reporter(path: str) -> bool:
    try:
        with open(path) as f:
            return KITTY_ASSET_MARKER in f.read(512)
    except OSError:
        return False


def opencode_plugin_config(agent: Agent) -> tuple[str, str]:
    base = os.path.dirname(agent.config_path())
    # OpenCode gives JSONC precedence over JSON. Update the active CLI config.
    for filename in ('cli.jsonc', 'cli.json'):
        path = os.path.join(base, filename)
        if os.path.exists(path):
            return path, 'plugins'
    return os.path.join(base, 'cli.json'), 'plugins'


def read_plugin_config(path: str) -> dict[str, Any]:
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        return {}
    # Keep quoted strings intact while removing JSONC comments and trailing commas.
    token = r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*[\s\S]*?\*/'
    text = re.sub(token, lambda m: m[0] if m[0].startswith('"') else ' ', text)
    text = re.sub(r'("(?:\\.|[^"\\])*")|,\s*(?=[}\]])', lambda m: m[1] or '', text)
    try:
        data = json.loads(text)
    except ValueError as err:
        raise SystemExit(f'Cannot read {path}: {err}') from err
    if not isinstance(data, dict):
        raise SystemExit(f'Cannot read {path}: expected a JSON object')
    return data


def legacy_reporter_path(agent: Agent) -> str:
    return os.path.join(os.path.dirname(agent.config_path()), 'kitty-agent-state.js')


def plugin_config_updates(agent: Agent, remove: bool = False) -> list[tuple[str, dict[str, Any]]]:
    path, key = opencode_plugin_config(agent)
    configs = [(path, key, './kitty-agent-state', remove)]
    legacy = legacy_reporter_path(agent)
    # An unowned legacy file and its registration belong to the user.
    if not os.path.lexists(legacy) or owned_reporter(legacy):
        base = os.path.dirname(agent.config_path())
        for filename in ('tui.jsonc', 'tui.json'):
            old_config = os.path.join(base, filename)
            if os.path.lexists(old_config):
                configs.append((old_config, 'plugin', './kitty-agent-state.js', True))
    updates = []
    for path, key, spec, removing in configs:
        data = read_plugin_config(path)
        entries = data.get(key, [])
        if not isinstance(entries, list):
            raise SystemExit(f'Cannot use {path}: {key} is not a list')
        kept = [entry for entry in entries if entry != spec]
        if not removing:
            kept.append(spec)
        if kept != entries:
            data[key] = kept
            if os.path.basename(path) == 'cli.json' and not os.path.exists(path):
                data.setdefault('$schema', 'https://opencode.ai/cli.json')
            updates.append((path, data))
    return updates


def register_plugin(agent: Agent, remove: bool = False) -> None:
    # Read and validate every affected config before writing any of them.
    updates = plugin_config_updates(agent, remove)
    for path, data in updates:
        write_json(path, data)
    legacy = legacy_reporter_path(agent)
    if owned_reporter(legacy):
        os.remove(legacy)


def reporter_is_installed(agent: Agent) -> bool:
    if not owned_reporter(reporter_path(agent)):
        return False
    if agent.integration_kind == 'tui-plugin':
        path, key = opencode_plugin_config(agent)
        entries = read_plugin_config(path).get(key, [])
        return isinstance(entries, list) and './kitty-agent-state' in entries
    return True


def write_hook(agent: Agent) -> str:
    path = hook_path(agent.id)
    if os.path.lexists(path) and not owned_hook(path):
        raise SystemExit(f'Refusing to overwrite an unowned hook: {path}')
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(path, 'w') as f:
        f.write(hook_text(agent))
    os.chmod(path, 0o700)
    return path


def read_json(path: str) -> dict[str, Any]:
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as err:
        raise SystemExit(f'Cannot read {path}: {err}') from err
    if not isinstance(data, dict):
        raise SystemExit(f'Cannot read {path}: expected a JSON object')
    return data


def write_json(path: str, data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        shutil.copy2(path, path + '.kitty-backup')
    tmp = path + '.kitty-tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
        f.write('\n')
    os.replace(tmp, path)


def is_kitty_hook(entry: Any) -> bool:
    return isinstance(entry, dict) and isinstance(entry.get('command'), str) and entry['command'].startswith(hook_dir() + os.sep)


def home_config_path(env_name: str, default: str, filename: str) -> Callable[[], str]:
    def resolve() -> str:
        return os.path.join(os.path.expanduser(os.environ.get(env_name) or default), filename)

    return resolve


claude_settings_path = home_config_path('CLAUDE_CONFIG_DIR', '~/.claude', 'settings.json')
codex_hooks_path = home_config_path('CODEX_HOME', '~/.codex', 'hooks.json')


def unsupported_config_path() -> str:
    return ''


def hook_entry(script: str) -> dict[str, Any]:
    return {'type': 'command', 'command': script, 'timeout': 5}


def register_hook(path: str, script: str) -> None:
    """Add a SessionStart command hook, keeping every other hook in the file.

    Claude Code's settings.json and Codex's hooks.json use the same shape: a
    top level hooks object maps an event to a list of matcher groups, each
    holding a list of handlers.
    """
    data = read_json(path)
    hooks = data.setdefault('hooks', {})
    if not isinstance(hooks, dict):
        raise SystemExit(f'Cannot use {path}: hooks is not an object')
    groups = hooks.setdefault('SessionStart', [])
    if not isinstance(groups, list):
        raise SystemExit(f'Cannot use {path}: hooks.SessionStart is not a list')
    for group in groups:
        entries = group.get('hooks') if isinstance(group, dict) else None
        if isinstance(entries, list):
            for i, entry in enumerate(entries):
                if isinstance(entry, dict) and entry.get('command') == script:  # update only this managed hook
                    entries[i] = hook_entry(script)
                    write_json(path, data)
                    return
    groups.append({'hooks': [hook_entry(script)]})
    write_json(path, data)


def unregister_hook(path: str) -> None:
    data = read_json(path)
    hooks = data.get('hooks')
    if not isinstance(hooks, dict) or not isinstance(hooks.get('SessionStart'), list):
        return
    kept = []
    for group in hooks['SessionStart']:
        if isinstance(group, dict) and isinstance(group.get('hooks'), list):
            group['hooks'] = [e for e in group['hooks'] if not is_kitty_hook(e)]
            if not group['hooks']:
                continue
        kept.append(group)
    if kept:
        hooks['SessionStart'] = kept
    else:
        hooks.pop('SessionStart', None)
    write_json(path, data)


def hook_is_installed(path: str) -> bool:
    hooks = read_json(path).get('hooks') if os.path.exists(path) else None
    if not isinstance(hooks, dict):
        return False
    for group in hooks.get('SessionStart') or ():
        if isinstance(group, dict) and any(is_kitty_hook(e) for e in group.get('hooks') or ()):
            return True
    return False


def inspect_integration(agent: Agent) -> IntegrationInspection:
    if agent.integration_kind not in ('hook', 'native', 'tui-plugin'):
        try:
            path = agent.config_path()
        except Exception as err:
            return _inspection_error((), err)
        return IntegrationInspection('unsupported', (path,), f'{agent.name} requires its native kitty adapter ({agent.integration_kind})')
    try:
        config = agent.config_path()
        paths = (config, hook_path(agent.id)) if agent.integration_kind == 'hook' else (reporter_path(agent),)
        if agent.integration_kind == 'tui-plugin':
            plugin_path, key = opencode_plugin_config(agent)
            paths = (plugin_path, reporter_path(agent))
        if not config:
            return IntegrationInspection('error', paths, 'No configuration path')
        parent = os.path.dirname(config)
        if not os.path.isdir(parent):
            return IntegrationInspection('missing', paths, f'{parent or "no config path"} does not exist')
        if agent.integration_kind == 'hook':
            script_path = hook_path(agent.id)
            script_exists = os.path.lexists(script_path)
            script_owned = owned_hook(script_path) if script_exists else False
            if script_exists and not script_owned:
                return IntegrationInspection('conflict', paths, f'Refusing to overwrite an unowned hook: {script_path}')
            try:
                data = read_json(config)
            except SystemExit as err:
                return _inspection_error(paths, err)
            hooks = data.get('hooks', {})
            if not isinstance(hooks, dict):
                return IntegrationInspection('error', paths, f'Cannot use {config}: hooks is not an object')
            groups = hooks.get(agent.hook_event, [])
            if not isinstance(groups, list):
                return IntegrationInspection('error', paths, f'Cannot use {config}: hooks.{agent.hook_event} is not a list')
            expected = hook_entry(script_path)
            registration = None
            registration_conflict = False
            for group in groups:
                if not isinstance(group, dict):
                    return IntegrationInspection('error', paths, f'Cannot use {config}: hook group is not an object')
                entries = group.get('hooks', [])
                if not isinstance(entries, list):
                    return IntegrationInspection('error', paths, f'Cannot use {config}: hook group hooks is not a list')
                for entry in entries:
                    if isinstance(entry, dict) and entry.get('command') == script_path:
                        registration = entry
                    elif isinstance(entry, dict) and entry.get('command', '').startswith(hook_dir() + os.sep):
                        registration_conflict = True
            if registration_conflict:
                return IntegrationInspection('conflict', paths, f'An unrelated kitty hook is registered in {config}')
            if not script_exists:
                return IntegrationInspection('missing', paths, 'Managed hook script is missing')
            if not os.path.isfile(script_path):
                return IntegrationInspection('error', paths, f'{script_path} is not a regular file')
            try:
                with open(script_path) as f:
                    script_current = f.read() == hook_text(agent)
            except OSError as err:
                return _inspection_error(paths, err)
            if not script_current or registration != expected:
                return IntegrationInspection('outdated', paths, 'Managed hook or registration differs from generated content')
            return IntegrationInspection('current', paths, 'Managed hook and registration are current')
        if agent.integration_kind == 'tui-plugin':
            directory = os.path.dirname(reporter_path(agent))
            if os.path.lexists(directory):
                if os.path.islink(directory) or not os.path.isdir(directory):
                    return IntegrationInspection('conflict', paths, f'Refusing to use an unowned reporter directory: {directory}')
            reporter = reporter_path(agent)
            if os.path.islink(reporter) or (os.path.lexists(reporter) and not os.path.isfile(reporter)):
                return IntegrationInspection('conflict', paths, f'Refusing to overwrite an unowned reporter: {reporter}')
            legacy = legacy_reporter_path(agent)
            if os.path.lexists(legacy) and (os.path.islink(legacy) or not owned_reporter(legacy)):
                return IntegrationInspection('conflict', paths, f'Refusing to migrate an unowned reporter: {legacy}')
            try:
                updates = plugin_config_updates(agent)
            except SystemExit as err:
                return _inspection_error(paths, err)
        reporter = reporter_path(agent)
        if not os.path.lexists(reporter):
            reporter_current = False
        elif not os.path.isfile(reporter):
            return IntegrationInspection('error', paths, f'{reporter} is not a regular file')
        elif not owned_reporter(reporter):
            return IntegrationInspection('conflict', paths, f'Refusing to overwrite an unowned reporter: {reporter}')
        else:
            try:
                with open(reporter) as f:
                    reporter_current = f.read() == reporter_text(agent)
            except OSError as err:
                return _inspection_error(paths, err)
        if agent.integration_kind == 'tui-plugin':
            try:
                data = read_plugin_config(plugin_path)
            except SystemExit as err:
                return _inspection_error(paths, err)
            entries = data.get(key, [])
            if not isinstance(entries, list):
                return IntegrationInspection('error', paths, f'Cannot use {plugin_path}: {key} is not a list')
            registered = './kitty-agent-state' in entries
            legacy_owned = owned_reporter(legacy_reporter_path(agent))
            if reporter_current and registered and not updates and not legacy_owned:
                return IntegrationInspection('current', paths, 'Managed reporter and plugin registration are current')
            if not reporter_current and not registered:
                state = 'outdated' if os.path.lexists(reporter) or legacy_owned else 'missing'
                return IntegrationInspection(state, paths, 'Managed reporter or plugin registration is missing or differs')
            return IntegrationInspection('outdated', paths, 'Managed reporter or plugin registration is missing or differs')
        if reporter_current:
            return IntegrationInspection('current', paths, 'Managed reporter is current')
        if os.path.exists(reporter):
            return IntegrationInspection('outdated', paths, 'Managed reporter differs from generated content')
        return IntegrationInspection('missing', paths, 'Managed reporter is missing')
    except Exception as err:
        return _inspection_error(locals().get('paths', ()), err)


AGENTS: tuple[Agent, ...] = (
    Agent('claude', 'Claude Code', 'CLAUDE_CODE_SESSION_ID', claude_settings_path, 'hook', config_env=('CLAUDE_CONFIG_DIR', '~/.claude')),
    Agent('codex', 'Codex', '', codex_hooks_path, 'hook', config_env=('CODEX_HOME', '~/.codex')),
    Agent('copilot', 'GitHub Copilot CLI', '', home_config_path('COPILOT_HOME', '~/.copilot', 'settings.json'), 'adapter-required'),
    Agent('devin', 'Devin CLI', '', home_config_path('XDG_CONFIG_HOME', '~/.config', 'devin/config.json'), 'adapter-required'),
    Agent('droid', 'Droid', '', home_config_path('FACTORY_HOME', '~/.factory', 'settings.json'), 'adapter-required'),
    Agent('kimi', 'Kimi Code CLI', '', home_config_path('KIMI_CODE_HOME', '~/.kimi-code', 'config.toml'), 'adapter-required'),
    Agent('qodercli', 'Qoder CLI', '', home_config_path('QODER_CONFIG_DIR', '~/.qoder', 'settings.json'), 'adapter-required'),
    Agent('qwen', 'Qwen Code', '', home_config_path('QWEN_HOME', '~/.qwen', 'settings.json'), 'adapter-required'),
    Agent('letta', 'Letta Code', '', home_config_path('LETTA_HOME', '~/.letta', 'settings.json'), 'adapter-required'),
    Agent('cursor', 'Cursor Agent CLI', '', home_config_path('CURSOR_CONFIG_DIR', '~/.cursor', 'hooks.json'), 'adapter-required'),
    Agent('grok', 'Grok CLI', '', home_config_path('GROK_HOME', '~/.grok', 'hooks/herdr.json'), 'adapter-required'),
    Agent('agy', 'Antigravity CLI', '', home_config_path('ANTIGRAVITY_CLI_CONFIG_DIR', '~/.gemini/config', 'hooks.json'), 'adapter-required'),
    Agent('mastracode', 'MastraCode', '', home_config_path('MASTRACODE_HOME', '~/.mastracode', 'hooks.json'), 'adapter-required'),
    Agent(
        'pi',
        'Pi',
        '',
        home_config_path('PI_CODING_AGENT_DIR', '~/.pi/agent', 'extensions/kitty-agent-state.ts'),
        'native',
        config_env=('PI_CODING_AGENT_DIR', '~/.pi/agent'),
    ),
    Agent(
        'omp',
        'OMP',
        '',
        home_config_path('PI_CODING_AGENT_DIR', '~/.omp/agent', 'extensions/kitty-agent-state.ts'),
        'native',
        config_env=('PI_CODING_AGENT_DIR', '~/.omp/agent'),
    ),
    Agent(
        'opencode',
        'OpenCode',
        '',
        home_config_path('XDG_CONFIG_HOME', '~/.config', 'opencode/cli.json'),
        'tui-plugin',
        config_env=('XDG_CONFIG_HOME', '~/.config'),
    ),
    Agent('kilo', 'Kilo Code CLI', '', home_config_path('XDG_CONFIG_HOME', '~/.config', 'kilo/plugin/herdr-agent-state.js'), 'plugin-required'),
    Agent('hermes', 'Hermes Agent', '', home_config_path('HERMES_HOME', '~/.hermes', 'plugins/herdr-agent-state/plugin.yaml'), 'plugin-required'),
)


def agent_by_id(agent_id: str) -> Agent:
    for agent in AGENTS:
        if agent.id == agent_id:
            return agent
    known = ', '.join(a.id for a in AGENTS)
    raise SystemExit(f'Unknown agent: {agent_id}. Known agents: {known}')


def install(agent_id: str) -> str:
    agent = agent_by_id(agent_id)
    inspection = inspect_integration(agent)
    if inspection.state in ('conflict', 'error'):
        raise SystemExit(inspection.detail)
    if agent.integration_kind == 'native':
        path = agent.config_path()
        if not path or not os.path.isdir(os.path.dirname(path)):
            raise SystemExit(f'{agent.name} is not configured on this computer: {os.path.dirname(path) or "no config path"} does not exist')
        return f'Installed the kitty session reporter for {agent.name} in {write_reporter(agent)}'
    if agent.integration_kind == 'tui-plugin':
        path = agent.config_path()
        if not path or not os.path.isdir(os.path.dirname(path)):
            raise SystemExit(f'{agent.name} is not configured on this computer: {os.path.dirname(path) or "no config path"} does not exist')
        reporter = write_reporter(agent)
        register_plugin(agent)
        return f'Installed the kitty session reporter for {agent.name} in {reporter}'
    if agent.integration_kind != 'hook':
        raise SystemExit(f'{agent.name} requires its native kitty adapter ({agent.integration_kind})')
    path = agent.config_path()
    if not path or not os.path.isdir(os.path.dirname(path)):
        raise SystemExit(f'{agent.name} is not configured on this computer: {os.path.dirname(path) or "no config path"} does not exist')
    script = write_hook(agent)
    register_hook(path, script)
    return f'Installed the kitty session hook for {agent.name} in {path}'


def uninstall(agent_id: str) -> str:
    agent = agent_by_id(agent_id)
    if agent.integration_kind in ('native', 'tui-plugin'):
        if agent.integration_kind == 'tui-plugin':
            inspection = inspect_integration(agent)
            if inspection.state in ('conflict', 'error'):
                raise SystemExit(inspection.detail)
            register_plugin(agent, remove=True)
        path = reporter_path(agent)
        if owned_reporter(path):
            os.remove(path)
            if agent.integration_kind == 'tui-plugin':
                directory = os.path.dirname(path)
                if not os.listdir(directory):
                    os.rmdir(directory)
        return f'Removed the kitty session reporter for {agent.name} from {path}'
    if agent.integration_kind != 'hook':
        return f'{agent.name} requires its native integration adapter; kitty has no generic hook to uninstall'
    path = agent.config_path()
    unregister_hook(path)
    script = hook_path(agent.id)
    if os.path.exists(script):
        os.remove(script)
    return f'Removed the kitty session hook for {agent.name} from {path}'


def status() -> list[str]:
    ans = []
    for agent in AGENTS:
        inspection = inspect_integration(agent)
        path = inspection.paths[0] if inspection.paths else ''
        ans.append(f'{agent.id:8} {inspection.state:11} {path} {inspection.detail}')
    return ans


def main(args: Sequence[str]) -> None:
    if len(args) < 2 or args[1] in ('-h', '--help'):
        names = ', '.join(a.id for a in AGENTS)
        raise SystemExit(f'Usage: kitty +agent-integration [install|uninstall] <agent>|status\n\nKnown agents: {names}')
    cmd = args[1]
    if cmd == 'status':
        print('\n'.join(status()))
        return
    if cmd not in ('install', 'uninstall') or len(args) < 3:
        raise SystemExit('Usage: kitty +agent-integration [install|uninstall] <agent>|status')
    print(install(args[2]) if cmd == 'install' else uninstall(args[2]))
