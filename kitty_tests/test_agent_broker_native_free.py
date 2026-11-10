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
import assert from 'node:assert/strict';
import {setTimeout as sleep} from 'node:timers/promises';
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
const handlers = new Map();
let dispose;
const api = {route: {current: {name: 'session', params: {sessionID: 'root'}}},
  state: {session: {status: () => ({type: 'busy'})}},
  event: {on: (type, fn) => {handlers.set(type, fn); return () => handlers.delete(type);}},
  lifecycle: {onDispose: fn => {dispose = fn;}}};
const emit = (type, properties) => handlers.get(type)({type, properties});
const wait = async predicate => {
  for (let i = 0; i < 150; i++) {if (predicate()) return; await sleep(20);}
  throw Error('timed out: ' + JSON.stringify(messages));
};
try {
  await (await import('./reporter.mjs')).default.tui(api);
  await wait(() => messages.some(m => m.event?.kind === 'session.status'));
  assert.equal(messages[0].type, 'register');
  assert.equal(messages[0].token, 'test-token');
  assert.equal(messages[0].protocol_version, 1);
  assert(!messages[0].capabilities.includes('turn.cancel'));
  assert.equal(messages[1].event.kind, 'gap');
  assert.equal(messages[1].event.payload.partial, true);
  assert.equal(messages.find(m => m.event?.kind === 'session.status').event.payload.snapshot, true);
  emit('session.created', {info: {id: 'child', parentID: 'root'}});
  emit('permission.asked', {sessionID: 'child', id: 'interaction', secret: 'private'});
  await wait(() => messages.some(m => m.event?.interaction_id === 'interaction'));
  assert.equal(messages.find(m => m.event?.session_id === 'child').event.parent_session_id, 'root');
  sockets[0].destroy();
  await sleep(40);
  emit('question.asked', {sessionID: 'root', id: 'during-disconnect'});
  await wait(() => messages.filter(m => m.type === 'register').length === 2);
  await wait(() => messages.filter(m => m.event?.kind === 'session.status').length === 2);
  const second = messages.findIndex((m, i) => i > 0 && m.type === 'register');
  assert.equal(messages[second + 1].event.kind, 'gap');
  assert.equal(messages[second + 1].event.payload.partial, true);
  assert(!JSON.stringify(messages).includes('private'));
  dispose();
  const count = sockets.length;
  await sleep(250);
  assert.equal(sockets.length, count);
  assert.equal(handlers.size, 0);
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
            result = subprocess.run(
                ['node', str(root / 'test.mjs')],
                capture_output=True,
                text=True,
                timeout=15,
                env=dict(os.environ, KITTY_WINDOW_TTY='/dev/null', KITTY_AGENT_BROKER_SOCKET=str(root / 'b.sock'), KITTY_AGENT_BROKER_TOKEN='test-token'),
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
