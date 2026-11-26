"""Run directly with python3 -m unittest kitty_tests.test_agent_broker_native_free."""

import ast
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from kitty.agent_provider import (
    OPENCODE_CAPABILITIES,
    OPENCODE_EVENT_TYPES,
    encode_opencode_event,
    normalize_opencode_event,
    opencode_broker_event,
    parse_opencode_event,
)

from . import requires_source_checkout


class OpenCodeContractTest(unittest.TestCase):
    @requires_source_checkout
    @unittest.skipUnless(shutil.which('node'), 'Node is required for reporter integration')
    def test_node_reporter(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'kitty/agent_integration.py').read_text())
        reporter = next(
            ast.literal_eval(n.value)
            for n in tree.body
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'OPENCODE_REPORTER' for t in n.targets)
        )
        reporter = reporter % {'id_var': 'agent', 'source_var': 'source', 'session_var': 'session'}
        script = r"""
import net from 'node:net';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import {setTimeout as sleep} from 'node:timers/promises';
const writeFileSync = fs.writeFileSync;
fs.writeFileSync = (path, data, ...args) => path === process.env.KITTY_WINDOW_TTY
  ? writeFileSync(path, data, {flag: 'a'}) : writeFileSync(path, data, ...args);
const messages = [], sockets = [];
const server = net.createServer(socket => {
  sockets.push(socket);
  let buffer = '';
  socket.on('data', data => {
    buffer += data;
    while (buffer.includes('\n')) {
      const end = buffer.indexOf('\n');
      messages.push(JSON.parse(buffer.slice(0, end)));
      buffer = buffer.slice(end + 1);
    }
  });
});
await new Promise(resolve => server.listen(process.env.KITTY_AGENT_BROKER_SOCKET, resolve));
const listeners = new Set();
let route = {type: 'session', sessionID: 'root'};
const sessions = new Map([
  ['root', {id: 'root'}], ['child', {id: 'child', parentID: 'root'}],
  ['other', {id: 'other'}], ['next', {id: 'next'}],
]);
const running = new Set(['root', 'child', 'other']);
const permissions = new Map(), forms = new Map(), pending = new Map();
const api = {ui: {router: {current: () => route}}, data: {
  session: {
    get: id => sessions.get(id),
    root: id => sessions.get(id)?.parentID || id,
    family: id => [...sessions.keys()].filter(key => key === id || sessions.get(key).parentID === id),
    status: id => running.has(id) ? 'running' : 'idle',
    permission: {list: id => permissions.get(id)}, form: {list: id => forms.get(id)},
    pending: {list: id => pending.get(id) || []},
  },
  listen: fn => {listeners.add(fn); return () => listeners.delete(fn);},
}};
const emit = (type, data) => {for (const fn of listeners) fn({details: {type, data}});};
const wait = async predicate => {
  for (let i = 0; i < 150; i++) {if (predicate()) return; await sleep(20);}
  throw Error('timed out: ' + JSON.stringify(messages));
};
const output = () => fs.readFileSync(process.env.KITTY_WINDOW_TTY, 'utf8');
const state = () => [...output().matchAll(/\x1b\]7501;([^\x07]+)\x07/g)].at(-1)?.[1];
const identity = id => output().includes(`SetUserVar=session=${Buffer.from(id).toString('base64')}\x07`);
const expectState = async (value, kind) => {
  await wait(() => state()?.includes(`state=${value}`));
  if (kind) assert(state().includes(`kind=${kind}`));
};
let dispose;
try {
  const plugin = (await import('./reporter.mjs')).default;
  dispose = plugin.setup(api);
  await wait(() => messages.some(m => m.event?.payload.snapshot));
  assert(identity('root'));
  await expectState('working');
  assert(state().includes('id=root/kitty'));
  assert.equal(messages[0].type, 'register');
  assert.equal(messages[0].token, 'test-token');
  assert.equal(messages[0].protocol_version, 1);
  assert(!messages[0].capabilities.includes('turn.cancel'));
  assert.equal(messages[1].event.kind, 'gap');
  assert.equal(messages[1].event.payload.partial, true);
  assert.equal(messages.find(m => m.event?.session_id === 'child').event.parent_session_id, 'root');
  permissions.set('child', [{id: 'interaction'}]);
  emit('permission.asked', {sessionID: 'child', id: 'interaction', secret: 'private'});
  await expectState('blocked', 'permission');
  await wait(() => messages.some(m => m.event?.interaction_id === 'interaction'));
  permissions.delete('child');
  emit('permission.replied', {sessionID: 'child', requestID: 'interaction'});
  await expectState('working');
  forms.set('root', [{id: 'form'}]);
  emit('form.created', {form: {sessionID: 'root', id: 'form', fields: ['private']}});
  await expectState('blocked', 'question');
  await wait(() => messages.some(m => m.event?.kind === 'question.asked' && m.event.interaction_id === 'form'));
  forms.delete('root');
  emit('form.cancelled', {sessionID: 'root', id: 'form'});
  await expectState('working');
  running.delete('root');
  emit('session.execution.succeeded', {sessionID: 'root'});
  await expectState('working'); // The already-running child still owns work.
  running.delete('child');
  emit('session.execution.interrupted', {sessionID: 'child', reason: 'user'});
  await expectState('idle'); // A different tab is still running, but is not ours.
  pending.set('root', [{type: 'user'}]);
  await expectState('working'); // Queued input must not look completed.
  pending.set('root', [{type: 'synthetic'}]);
  await expectState('idle');
  sessions.get('root').outcome = 'failed';
  emit('session.execution.failed', {sessionID: 'root', error: 'private'});
  await expectState('error');
  running.add('root');
  emit('session.execution.started', {sessionID: 'root'});
  await expectState('working');
  const snapshots = messages.filter(m => m.event?.payload.snapshot).length;
  sockets.at(-1).destroy();
  await sleep(40);
  await wait(() => messages.filter(m => m.event?.payload.snapshot).length > snapshots);
  const second = messages.findIndex((m, i) => i > 0 && m.type === 'register');
  assert.equal(messages[second + 1].event.kind, 'gap');
  assert.equal(messages[second + 1].event.payload.partial, true);
  route = {type: 'session', sessionID: 'next'};
  await wait(() => identity('next'));
  await expectState('idle');
  assert(output().includes('state=clear:id=root/kitty'));
  route = {type: 'home'};
  await wait(() => state() === 'state=clear:id=next/kitty');
  await wait(() => output().endsWith('\x1b]1337;SetUserVar=session\x07'));
  assert(!JSON.stringify(messages).includes('private'));
  dispose();
  await sleep(20);
  const count = sockets.length, bytes = output().length;
  await sleep(250);
  assert.equal(sockets.length, count);
  assert.equal(output().length, bytes);
  assert.equal(listeners.size, 0);
} finally {
  dispose?.();
  for (const socket of sockets) socket.destroy();
  await new Promise(resolve => server.close(resolve));
}
"""
        import os

        with tempfile.TemporaryDirectory(prefix='provider-') as directory:
            root = Path(directory)
            (root / 'reporter.mjs').write_text(reporter)
            (root / 'test.mjs').write_text(script)
            tty = root / 'tty'
            tty.touch()
            result = subprocess.run(
                ['node', str(root / 'test.mjs')],
                capture_output=True,
                text=True,
                timeout=15,
                env=dict(os.environ, KITTY_WINDOW_TTY=str(tty), KITTY_AGENT_BROKER_SOCKET=str(root / 'b.sock'), KITTY_AGENT_BROKER_TOKEN='test-token'),
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_broker_protocol(self):
        event = {'type': 'permission.asked', 'properties': {'sessionID': 's', 'id': 'i', 'secret': 'private'}}
        self.assertEqual(
            opencode_broker_event(event, '1'),
            {'type': 'event', 'event': {'kind': 'permission.asked', 'session_id': 's', 'interaction_id': 'i', 'payload': {}, 'event_id': '1'}},
        )
        self.assertNotIn('turn.cancel', OPENCODE_CAPABILITIES)

    def test_all_events_round_trip(self):
        for event_type in OPENCODE_EVENT_TYPES:
            with self.subTest(event_type=event_type):
                properties = {
                    'sessionID': 'child',
                    'info': {'id': 'child', 'parentID': 'parent'},
                    'id': 'interaction',
                    'requestID': 'interaction',
                    'status': {'type': 'busy'},
                    'secret': 'not forwarded',
                    'answers': ['private'],
                    'error': {'message': 'private'},
                }
                event = {'type': event_type, 'properties': properties}
                line = encode_opencode_event(event)
                self.assertIsNotNone(line)
                self.assertEqual(line.count('\n'), 1)
                normalized = parse_opencode_event(line)
                self.assertEqual(normalized, normalize_opencode_event(event))
                self.assertEqual(normalized['parent_session_id'], 'parent')
                self.assertNotIn('private', line)
                self.assertNotIn('secret', line)
                if event_type.startswith(('permission.', 'question.')):
                    self.assertEqual(normalized['interaction_id'], 'interaction')

    def test_created_deleted_and_status(self):
        for kind in ('session.created', 'session.deleted'):
            value = normalize_opencode_event({'type': kind, 'properties': {'info': {'id': 's', 'parentID': 'p'}}})
            self.assertEqual(value['session_id'], 's')
        for status, state in (('busy', 'working'), ('retry', 'working'), ('idle', 'idle')):
            value = normalize_opencode_event({'type': 'session.status', 'properties': {'sessionID': 's', 'status': status}})
            self.assertEqual(value['state'], state)
        value = normalize_opencode_event({'type': 'question.asked', 'properties': {'sessionID': 's'}})
        self.assertNotIn('interaction_id', value)
        self.assertNotIn('parent_session_id', value)

    def test_invalid_and_unknown(self):
        for event in (
            {},
            {'type': []},
            {'type': 'tool.updated'},
            {'type': 'session.error', 'properties': {}},
            {'type': 'session.status', 'properties': {'sessionID': 's', 'status': []}},
        ):
            self.assertIsNone(normalize_opencode_event(event))
            self.assertIsNone(encode_opencode_event(event))
        valid = json.loads(encode_opencode_event({'type': 'session.idle', 'properties': {'sessionID': 's'}}))
        for patch in (
            {'version': 2},
            {'version': True},
            {'provider': 'other'},
            {'secret': 'x'},
            {'state': 'blocked'},
            {'interaction_id': 'invented'},
            {'session_id': ''},
        ):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                parse_opencode_event(json.dumps(valid | patch))
        for line in ('{}', '[]', 'invalid', '{}\n{}', b'\xff'):
            with self.assertRaises(ValueError):
                parse_opencode_event(line)
        self.assertNotIn('transcript', OPENCODE_CAPABILITIES)
        self.assertNotIn('tool', OPENCODE_CAPABILITIES)


if __name__ == '__main__':
    unittest.main()
