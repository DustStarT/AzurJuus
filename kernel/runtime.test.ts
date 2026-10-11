import assert from "node:assert/strict";
import test from "node:test";
import { PluginKernel } from "./runtime.ts";
import { HttpRuntimeEventSource, runtimeEventsPlugin } from "./runtime-events.ts";
import { workflowPlugin } from "./plugins/workflows.ts";
import type { WorkflowService } from "./plugins/workflows.ts";
import { KernelHost } from "./host.ts";
import type { MessageService } from "./plugins/messages.ts";
import type { DomainService } from "./plugins/domains.ts";

test("plugins wait for injected services and dispose registrations", async () => {
  const kernel = new PluginKernel();
  const events: string[] = [];
  kernel.use({
    name: "state",
    apply(ctx) {
      ctx.provide("state", { value: 7 });
    },
  });
  kernel.use({
    name: "observer",
    inject: ["state"],
    apply(ctx) {
      assert.equal(ctx.require<{ value: number }>("state").value, 7);
      ctx.on<{ message: string }>("test/message", payload => { events.push(payload.message); });
    },
  });
  await kernel.start();
  await kernel.events.emit("test/message", { message: "ready" });
  assert.deepEqual(events, ["ready"]);
  assert.equal(kernel.hasService("state"), true);
  await kernel.stop();
  assert.equal(kernel.hasService("state"), false);
  await kernel.events.emit("test/message", { message: "ignored" });
  assert.deepEqual(events, ["ready"]);
});

test("unresolved plugin dependencies fail before partial startup", async () => {
  const kernel = new PluginKernel().use({
    name: "missing",
    inject: ["never-provided"],
    apply() {},
  });
  await assert.rejects(kernel.start(), /Unresolved plugin dependencies/);
});

test("failed startup disposes the official plugin fiber and previously provided services", async () => {
  let released = false;
  const kernel = new PluginKernel()
    .use({ name: 'resource', apply(ctx) { ctx.provide('resource', {}); } })
    .use({ name: 'missing', inject: ['missing-service'], apply() {} });
  kernel.use({ name: 'cleanup', inject: ['resource'], apply(ctx) { ctx.effect(() => { released = true; }); } });
  await assert.rejects(kernel.start(), /Unresolved plugin dependencies/);
  assert.equal(released, true);
  assert.equal(kernel.hasService('resource'), false);
});

test("runtime events project RunStore state into workflow plugins", async () => {
  const requests: string[] = [];
  const source = new HttpRuntimeEventSource({
    endpoint: "http://127.0.0.1/api/runtime/events",
    fetchImpl: async input => {
      requests.push(String(input));
      return new Response(JSON.stringify({ events: [
        { seq: 1, runId: "r1", type: "run.created", payload: { status: "queued", prompt: "hello" }, at: 10 },
        { seq: 2, runId: "r1", type: "tool.completed", payload: { callId: "c1", name: "echo", result: "ok" }, at: 11 },
      ] }), { status: 200 });
    },
  });
  const kernel = new PluginKernel().use(runtimeEventsPlugin({ source })).use(workflowPlugin());
  const names: string[] = [];
  kernel.use({ name: "workflow-observer", inject: ["workflows"], apply(ctx) {
    ctx.on("workflow/created", () => { names.push("created"); });
    ctx.on("workflow/tool", () => { names.push("tool"); });
  }});
  await kernel.start();
  const runtime = kernel.getService<import("./runtime-events.ts").RuntimeEventService>("runtime-events")!;
  kernel.getService<WorkflowService>("workflows")!.hydrate([{ id: "seed", status: "paused", updatedAt: 9 }]);
  assert.equal(kernel.getService<WorkflowService>("workflows")!.get("seed")?.status, "paused");
  assert.equal((await runtime.sync()).length, 2);
  const workflow = kernel.getService<WorkflowService>("workflows")!.get("r1")!;
  assert.equal(workflow.status, "queued");
  assert.equal(workflow.calls.c1.name, "echo");
  assert.equal(workflow.data.callId, undefined);
  assert.deepEqual(names, ["created", "tool"]);
  assert.equal(await runtime.ingest({ seq: 2, runId: "r1", type: "tool.completed", payload: {}, at: 12 }), false);
  await runtime.ingest({ seq: 3, runId: "r1", type: "tool.output", payload: { callId: "c1", text: "line" }, at: 12 });
  assert.equal(kernel.getService<WorkflowService>("workflows")!.get("r1")!.outputs.c1, "line");
  await runtime.ingest({ seq: 4, runId: "seed", type: "run.removed", payload: {}, at: 13 });
  assert.equal(kernel.getService<WorkflowService>("workflows")!.get("seed"), undefined);
  assert.match(requests[0], /after=0/);
  await kernel.stop();
});

test("kernel host composes the migration plugins behind one lifecycle", async () => {
  const host = new KernelHost({
    runtimeSource: {
      async read(after) {
        return after === 0 ? [{ seq: 1, runId: "r2", type: "run.created", payload: { status: "queued" }, at: 20 }] : [];
      },
    },
  });
  await host.start();
  await host.syncRuntime();
  assert.equal(host.kernel.getService<WorkflowService>("workflows")!.get("r2")?.status, "queued");
  assert.equal(host.kernel.hasService("messages"), true);
  assert.equal(host.kernel.hasService("domains"), true);
  await host.stop();
  assert.equal(host.kernel.hasService("workflows"), false);
});

test("domain projection routes mind, skill and social signals by actor", async () => {
  const host = new KernelHost({ runtimeSource: { async read() { return []; } } });
  await host.start();
  const runtime = host.kernel.getService<import("./runtime-events.ts").RuntimeEventService>("runtime-events")!;
  const domains = host.kernel.getService<DomainService>("domains")!;
  const seen: string[] = [];
  const unsubscribe = domains.subscribe(signal => seen.push(`${signal.kind}:${signal.actorId ?? "global"}`));
  await runtime.ingest({ seq: 1, runId: null, type: "mind.changed", payload: { actorId: "a", version: 2 }, at: 1 });
  await runtime.ingest({ seq: 2, runId: null, type: "skill.changed", payload: { actorId: "a", skillId: "s" }, at: 2 });
  await runtime.ingest({ seq: 3, runId: null, type: "social.message", payload: { actorId: "b", messageId: "m" }, at: 3 });
  assert.deepEqual(seen, ["mind:a", "skill:a", "social:b"]);
  assert.equal(domains.latest("mind", "a")?.payload.version, 2);
  assert.equal(domains.list("social").length, 1);
  unsubscribe();
  await host.stop();
});

test("message streams survive completion until identical persisted content arrives", async () => {
  const host = new KernelHost({ runtimeSource: { async read() { return []; } } });
  await host.start();
  const runtime = host.kernel.getService<import("./runtime-events.ts").RuntimeEventService>("runtime-events")!;
  const messages = host.kernel.getService<MessageService>("messages")!;
  const payload = { actorId: "a", assignmentId: "chat", messageId: "m1" };
  await runtime.ingest({ seq: 1, runId: "r1", type: "message.start", payload, at: 10 });
  await runtime.ingest({ seq: 2, runId: "r1", type: "message.delta", payload: { ...payload, delta: "hello" }, at: 11 });
  assert.equal(await runtime.ingest({ seq: 2, runId: "r1", type: "message.delta", payload: { ...payload, delta: "hello" }, at: 11 }), false);
  assert.equal(messages.get("r1:m1")?.text, "hello");
  await runtime.ingest({ seq: 3, runId: "r1", type: "message.complete", payload: { ...payload, text: "hello world", expression: true }, at: 12 });
  assert.equal(messages.get("r1:m1")?.text, "hello world");
  assert.equal(messages.get("r1:m1")?.done, true);
  assert.equal(messages.get("r1:m1")?.expression, true);
  assert.equal(messages.get("r1:m1")?.createdAt, new Date(10000).toISOString());
  messages.prune([{ id: "m1", body: "old content" }]);
  assert.equal(messages.get("r1:m1")?.text, "hello world");
  messages.prune([{ id: "m1", body: "hello world" }]);
  assert.equal(messages.get("r1:m1"), undefined);
  await messages.apply({ seq: 3, runId: "r1", type: "message.complete", payload: { ...payload, text: "hello world" }, at: 12 });
  assert.equal(messages.get("r1:m1"), undefined);
  await host.stop();
  assert.deepEqual(messages.list(), {});
});

test("message streams isolate runs and keep interrupted text available", async () => {
  const host = new KernelHost({ runtimeSource: { async read() { return []; } } });
  await host.start();
  const runtime = host.kernel.getService<import("./runtime-events.ts").RuntimeEventService>("runtime-events")!;
  const messages = host.kernel.getService<MessageService>("messages")!;
  const changes: string[] = [];
  const unsubscribe = messages.subscribe(key => { changes.push(key); });
  await runtime.ingest({ seq: 1, runId: "r1", type: "message.delta", payload: { messageId: "m1", delta: "partial" }, at: 1 });
  await runtime.ingest({ seq: 2, runId: "r2", type: "message.delta", payload: { messageId: "m2", delta: "other" }, at: 2 });
  await runtime.ingest({ seq: 3, runId: "r1", type: "run.updated", payload: { status: "paused" }, at: 3 });
  assert.equal(messages.get("r1:m1")?.text, "partial");
  assert.equal(messages.get("r1:m1")?.done, true);
  assert.equal(messages.get("r2:m2")?.done, false);
  messages.prune([{ id: "m1", body: "partial" }, { id: "m2", body: "other" }]);
  assert.equal(messages.get("r1:m1"), undefined);
  assert.equal(messages.get("r2:m2")?.text, "other");
  unsubscribe();
  const notified = changes.length;
  await runtime.ingest({ seq: 4, runId: "r2", type: "run.removed", payload: {}, at: 4 });
  assert.equal(messages.get("r2:m2"), undefined);
  assert.equal(changes.length, notified);
  await host.stop();
});
