import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import test from 'node:test';
import { apply } from './workspace.mjs';

function mount(endpoint, catalog, services = {}) {
  let tool, disposed = false;
  const effects = [];
  apply({ tools: { register(def) { tool = def; return () => { disposed = true; }; } },
    get(name) { return services[name]; }, effect(factory) { effects.push(factory()); } },
    { endpoint, catalog, token: 'synthetic-token' });
  return { tool, dispose() { effects.forEach(effect => effect()); }, get disposed() { return disposed; } };
}

async function gateway(t, handle) {
  const server = createServer(handle);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  return `http://127.0.0.1:${server.address().port}/tool`;
}

test('native tool enforces its phase catalog and releases registration', async t => {
  let calls = 0;
  const endpoint = await gateway(t, async (req, res) => {
    let data = '';
    for await (const part of req) data += part;
    const body = JSON.parse(data);
    assert.equal(req.headers.authorization, 'Bearer synthetic-token');
    assert.match(body.callId, /^[a-f0-9]{64}$/);
    calls++;
    res.end(JSON.stringify({ ...body, status: 'completed', result: { files: [] } }));
  });
  const plugin = mount(endpoint, { list_dir: 'Read directory.' });
  assert.deepEqual(plugin.tool.parameters.properties.action.enum, ['list_dir']);
  await assert.rejects(plugin.tool.execute({ action: 'command', args: {} }, {}), /unauthorized/);
  const result = await plugin.tool.execute({ action: 'list_dir', args: {} }, { callId: 'native_call-id' });
  assert.equal(result.status, 'completed');
  assert.equal(calls, 1);
  plugin.dispose();
  assert.equal(plugin.disposed, true);
});

test('chat has no tools and remote gateways are rejected', () => {
  assert.equal(mount('http://localhost:1/tool', {}).tool, undefined);
  assert.throws(() => mount('https://example.com/tool', { list_dir: 'Read.' }), /must be local/);
});

test('gateway permission reasons and cancellation reach the native executor', async t => {
  const endpoint = await gateway(t, (_req, res) => {
    res.writeHead(403); res.end(JSON.stringify({ detail: 'Read-only reviewer cannot run commands' }));
  });
  const tool = mount(endpoint, { command: 'Run program.' }).tool;
  await assert.rejects(tool.execute({ action: 'command', args: {} }, { callId: 'denied' }), /Read-only reviewer/);
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(tool.execute({ action: 'command', args: {} }, { callId: 'cancelled', signal: controller.signal }), /abort/i);
});

test('durable image projection does not bypass a replaced policy result', async t => {
  const endpoint = await gateway(t, (_req, res) => res.end(JSON.stringify({ status: 'completed',
    callId: 'image', result: { image: 'aGVsbG8=', mimeType: 'image/png', sha256: 'source' } })));
  const tool = mount(endpoint, { read_image: 'Read pixels.' }, {
    llm: { async resolveModelInfo() { return { inputModalities: ['text', 'image'] }; } },
    attachments: { async saveImages() { return [{ id: 'durable-image' }]; } },
  }).tool;
  const exec = { callId: 'image', agent: { session: { requestHeader() { return { config: { model: 'vision', provider: 'test' } }; } } } };
  const value = await tool.execute({ action: 'read_image', args: {} }, exec);
  assert.equal(tool.projectContent(exec, { value, isError: false })[1].type, 'image');
  await tool.execute({ action: 'read_image', args: {} }, exec);
  assert.equal(tool.projectContent(exec, { value: { status: 'rejected' }, isError: false }), undefined);
});

test('image results require a capable active model', async t => {
  const endpoint = await gateway(t, (_req, res) => res.end(JSON.stringify({ status: 'completed',
    result: { image: 'aGVsbG8=', mimeType: 'image/png' } })));
  const tool = mount(endpoint, { read_image: 'Read pixels.' }).tool;
  await assert.rejects(tool.execute({ action: 'read_image', args: {} }, { callId: 'text-model' }), /cannot admit image/);
});
