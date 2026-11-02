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
PI_REPORTER = r'''// KITTY_AGENT_INTEGRATION_ID=kitty
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
'''

OPENCODE_REPORTER = r'''// KITTY_AGENT_INTEGRATION_ID=kitty
import fs from "node:fs";
const AGENT = "opencode";
const SOURCE = "kitty:opencode";
const ID_KEY = "%(id_var)s";
const SOURCE_KEY = "%(source_var)s";
const SESSION_KEY = "%(session_var)s";
const tty = process.env.KITTY_WINDOW_TTY;
let currentSession;
const childSessions = new Set();
const lastStates = new Map();
let writeChain = Promise.resolve();

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

function statusRecordID(sessionID) {
  return sessionID === currentSession ? currentSession : `${currentSession}/${sessionID}`;
}

function reportStatus(sessionID, state, kind) {
  if (!sessionID || !tty) return;
  const recordID = statusRecordID(sessionID);
  if (lastStates.get(recordID) === state && !kind) return;
  lastStates.set(recordID, state);
  const fields = [`id=${recordID}`, `state=${state}`, "app=opencode"];
  if (kind) fields.splice(2, 0, `kind=${kind}`);
  write(`\x1b]7501;${fields.join(":")}\x07`);
}

function clearStatus(sessionID = currentSession) {
  if (!sessionID) return;
  const recordID = sessionID === currentSession ? currentSession : statusRecordID(sessionID);
  write(`\x1b]7501;state=clear:id=${recordID}\x07`);
  if (sessionID === currentSession) lastStates.clear();
  else lastStates.delete(recordID);
}

function routeSession(api) {
  const route = api.route.current;
  return route?.name === "session" && typeof route.params?.sessionID === "string"
    ? route.params.sessionID : undefined;
}

function statusOf(value) {
  return typeof value === "string" ? value : value?.type;
}

function isTrackedSession(sessionID) {
  return sessionID === currentSession || childSessions.has(sessionID);
}

function sync(api) {
  const sessionID = routeSession(api);
  if (sessionID === currentSession) return;
  clearStatus(currentSession);
  childSessions.clear();
  currentSession = sessionID;
  lastStates.clear();
  reportIdentity(sessionID);
  const status = sessionID ? statusOf(api.state.session.status(sessionID)) : undefined;
  if (status === "busy" || status === "retry") reportStatus(sessionID, "working");
  else if (status === "idle") reportStatus(sessionID, "idle");
}

export default { id: "kitty.opencode.session", tui: async (api) => {
  if (!tty) return;
  const onStatus = (event) => {
    const properties = event?.properties ?? {};
    const info = properties.info;
    const createdID = info?.id;
    const parentID = info?.parentID;
    if (event.type === "session.created" && createdID && (parentID === currentSession || childSessions.has(parentID))) {
      childSessions.add(createdID);
      return;
    }
    const sessionID = properties.sessionID || createdID;
    if (!sessionID || !isTrackedSession(sessionID)) return;
    const type = event.type;
    const status = statusOf(properties.status);
    if (type === "session.status") {
      if (status === "busy" || status === "retry") reportStatus(sessionID, "working");
      else if (status === "idle") reportStatus(sessionID, "idle");
    } else if (type === "session.idle") reportStatus(sessionID, "idle");
    else if (type === "permission.asked") reportStatus(sessionID, "blocked", "permission");
    else if (type === "question.asked") reportStatus(sessionID, "blocked", "question");
    else if (type === "permission.replied" || type === "question.replied" || type === "question.rejected") reportStatus(sessionID, "working");
    else if (type === "session.error") reportStatus(sessionID, "error");
    else if (type === "session.deleted") {
      clearStatus(sessionID);
      if (sessionID === currentSession) childSessions.clear();
      else childSessions.delete(sessionID);
    }
  };
  const off = ["session.created", "session.status", "session.idle", "permission.asked", "question.asked", "permission.replied", "question.replied", "question.rejected", "session.error", "session.deleted"].map((type) => api.event.on(type, onStatus));
  sync(api);
  const timer = setInterval(() => sync(api), 100);
  api.lifecycle.onDispose(() => { clearInterval(timer); for (const remove of off) remove(); clearStatus(); reportIdentity(undefined); });
}};
'''
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


def hook_dir() -> str:
    return os.path.join(config_dir, HOOK_DIR_NAME)


def hook_path(agent: str) -> str:
    return os.path.join(hook_dir(), f'{agent}-session.sh')


def reporter_path(agent: Agent) -> str:
    if agent.integration_kind == 'native':
        return os.path.join(os.path.dirname(agent.config_path()), 'kitty-agent-state.ts')
    return os.path.join(os.path.dirname(agent.config_path()), 'kitty-agent-state.js')


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
    if os.path.exists(path) and not owned_reporter(path):
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
    # OpenCode's TUI loads plugins from tui.json[c], using the singular
    # `plugin` key. It does not load cli.json plugins in the TUI.
    for filename in ('tui.json', 'tui.jsonc'):
        path = os.path.join(base, filename)
        if os.path.exists(path):
            return path, 'plugin'
    return os.path.join(base, 'tui.json'), 'plugin'


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


def register_plugin(agent: Agent, remove: bool = False) -> None:
    path, key = opencode_plugin_config(agent)
    data = read_plugin_config(path)
    entries = data.get(key, [])
    if not isinstance(entries, list):
        raise SystemExit(f'Cannot use {path}: {key} is not a list')
    spec = './kitty-agent-state.js'
    kept = [entry for entry in entries if entry != spec]
    if not remove:
        kept.append(spec)
    if kept == entries:
        return
    data[key] = kept
    if os.path.basename(path) == 'tui.json' and '$schema' not in data:
        data['$schema'] = 'https://opencode.ai/tui.json'
    write_json(path, data)


def reporter_is_installed(agent: Agent) -> bool:
    if not owned_reporter(reporter_path(agent)):
        return False
    if agent.integration_kind == 'tui-plugin':
        path, key = opencode_plugin_config(agent)
        entries = read_plugin_config(path).get(key, [])
        return isinstance(entries, list) and './kitty-agent-state.js' in entries
    return True


def write_hook(agent: Agent) -> str:
    path = hook_path(agent.id)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    script = HOOK_SCRIPT % {
        'agent': agent.id,
        'id_env': agent.id_env or 'KITTY_UNSET_SESSION_ID',
        'session_var': AGENT_SESSION_VAR,
        'agent_var': AGENT_ID_VAR,
        'source_var': AGENT_SOURCE_VAR,
    }
    with open(path, 'w') as f:
        f.write(script)
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
    return isinstance(entry, dict) and 'kitty' in str(entry.get('command', ''))


def claude_settings_path() -> str:
    return os.path.join(os.path.expanduser(os.environ.get('CLAUDE_CONFIG_DIR') or '~/.claude'), 'settings.json')


def codex_hooks_path() -> str:
    return os.path.join(os.path.expanduser(os.environ.get('CODEX_HOME') or '~/.codex'), 'hooks.json')


def home_config_path(env_name: str, default: str, filename: str) -> Callable[[], str]:
    def resolve() -> str:
        return os.path.join(os.path.expanduser(os.environ.get(env_name) or default), filename)

    return resolve


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
                if is_kitty_hook(entry):  # already installed, update it in place
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


AGENTS: tuple[Agent, ...] = (
    Agent('claude', 'Claude Code', 'CLAUDE_CODE_SESSION_ID', claude_settings_path, 'hook'),
    Agent('codex', 'Codex', '', codex_hooks_path, 'hook'),
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
    Agent('pi', 'Pi', '', home_config_path('PI_CODING_AGENT_DIR', '~/.pi/agent', 'extensions/kitty-agent-state.ts'), 'native'),
    Agent('omp', 'OMP', '', home_config_path('PI_CODING_AGENT_DIR', '~/.omp/agent', 'extensions/kitty-agent-state.ts'), 'native'),
    Agent('opencode', 'OpenCode', '', home_config_path('XDG_CONFIG_HOME', '~/.config', 'opencode/kitty-agent-state.js'), 'tui-plugin'),
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
    if agent.integration_kind in ('native', 'tui-plugin'):
        path = agent.config_path()
        if not path or not os.path.isdir(os.path.dirname(path)):
            raise SystemExit(f'{agent.name} is not configured on this computer: {os.path.dirname(path) or "no config path"} does not exist')
        if agent.integration_kind == 'tui-plugin':
            register_plugin(agent)
        return f'Installed the kitty session reporter for {agent.name} in {write_reporter(agent)}'
    if agent.integration_kind != 'hook':
        raise SystemExit(f'{agent.name} requires its native kitty adapter ({agent.integration_kind})')
    path = agent.config_path()
    if not path or not os.path.isdir(os.path.dirname(path)):
        raise SystemExit(f'{agent.name} is not configured on this computer: {os.path.dirname(path) or "no config path"} does not exist')
    register_hook(path, write_hook(agent))
    return f'Installed the kitty session hook for {agent.name} in {path}'


def uninstall(agent_id: str) -> str:
    agent = agent_by_id(agent_id)
    if agent.integration_kind in ('native', 'tui-plugin'):
        if agent.integration_kind == 'tui-plugin':
            register_plugin(agent, remove=True)
        path = reporter_path(agent)
        if owned_reporter(path):
            os.remove(path)
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
        if agent.integration_kind in ('native', 'tui-plugin'):
            state = 'installed' if reporter_is_installed(agent) else 'not installed'
            ans.append(f'{agent.id:8} {state:14} {reporter_path(agent)}')
            continue
        if agent.integration_kind == 'plugin-required':
            ans.append(f'{agent.id:8} plugin-required {agent.config_path()}')
            continue
        path = agent.config_path()
        state = 'installed' if hook_is_installed(path) else 'not installed'
        ans.append(f'{agent.id:8} {state:14} {path}')
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
