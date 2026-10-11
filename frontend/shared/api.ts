import { ref, shallowRef, provide } from "vue";
import type { Workspace, Run, RunEvent } from "./types";
import { liveSpeechIds } from '../features/chat/speechQueue';
import { preloadAssets } from "./assets";
import { createKernelRuntime, kernelRuntimeKey } from "./kernel-runtime";
import type { WorkflowDetail } from "./kernel-runtime";

export async function api<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!response.ok) {
    const error = await response
      .json()
      .catch(() => ({ detail: response.statusText }));
    throw new Error(
      typeof error.detail === "string"
        ? error.detail
        : JSON.stringify(error.detail),
    );
  }
  return response.json();
}

export function useWorkspace() {
  const workspace = shallowRef<Workspace>();
  const kernel = createKernelRuntime();
  provide(kernelRuntimeKey, kernel);
  const runs = kernel.runs;
  const online = ref(false);
  const error = ref("");
  const streams = kernel.streams;
  let cursor = 0,
    socket: WebSocket | undefined,
    reconnect = 0,
    stopped = false,
    refreshTimer = 0;
  let backoff = 500,
    batch: RunEvent[] = [],
    frame = 0;
  let initialized = false;
  let eventQueue: Promise<void> = Promise.resolve();
  let refreshGeneration = 0;
  async function refresh() {
    const generation = ++refreshGeneration;
    try {
      const [boot, active] = await Promise.all([
        api<{ workspace: Workspace }>("/api/bootstrap"),
        api<{ runs: Run[]; cursor: number }>("/api/runtime/state"),
      ]);
      if (generation !== refreshGeneration || stopped) return;
      workspace.value = boot.workspace;
      preloadAssets(boot.workspace.data.agents);
      kernel.hydrateRuns(active.runs, active.cursor);
      if (!initialized) {
        cursor = active.cursor;
        initialized = true;
      }
      const saved = Object.values(boot.workspace.data.messages).flat();
      kernel.pruneMessages(saved.map(message => ({ id: message.id, body: message.body })));
      error.value = "";
    } catch (e) {
      if (generation !== refreshGeneration) return;
      error.value = (e as Error).message;
      throw e;
    }
  }
  function scheduleRefresh() {
    if (!refreshTimer)
      refreshTimer = window.setTimeout(() => {
        refreshTimer = 0;
        void refresh().catch(() => {});
      }, 250);
  }
  function flush() {
    frame = 0;
    const pending = batch;
    batch = [];
    eventQueue = eventQueue.then(() => processEvents(pending)).catch(e => {
      if (!stopped) {
        error.value = (e as Error).message;
        scheduleRefresh();
      }
    });
  }
  async function processEvents(pending: RunEvent[]) {
    for (const event of pending) {
      if (stopped) return;
      if (event.type === "runtime.sync") {
        cursor = event.seq;
        scheduleRefresh();
        continue;
      }
      if (event.seq <= cursor) continue;
      if (cursor && event.seq !== cursor + 1) scheduleRefresh();
      cursor = event.seq;
      try {
        await kernel.ingest(event);
        if (stopped) return;
      } catch (e) {
        error.value = (e as Error).message;
        scheduleRefresh();
        continue;
      }
      if (event.type === 'mind.changed') {
        window.dispatchEvent(new CustomEvent('azur-mind-changed', {detail:event.payload}));
      } else if (event.type === 'mind.observation') {
        // The cognition worker normally follows observations with mind.changed.
        // Notify the panel from the source event as well so a freshly opened tab
        // does not miss a refresh while the projection event is being queued.
        const actorIds = Array.isArray((event.payload as {actorIds?: unknown}).actorIds)
          ? (event.payload as {actorIds: unknown[]}).actorIds
          : [];
        for (const actorId of actorIds) {
          window.dispatchEvent(new CustomEvent('azur-mind-changed', {detail:{actorId}}));
        }
      }
      if (event.type === "run.updated" || event.type === "run.created") {
        const run = runs.value.find(item => item.id === event.runId);
        if (run && ["completed", "failed", "cancelled", "paused"].includes(run.status)) {
          scheduleRefresh();
        }
      } else if (event.type === 'social.message') {
        liveSpeechIds.add(String(event.payload.messageId));
        scheduleRefresh();
      } else if (event.type === 'social.reply_deferred') {
        scheduleRefresh();
      } else if (event.type === "message.complete") {
        const messageId = String(event.payload.messageId || event.runId + ":" + event.payload.assignmentId);
        if (event.payload.expression) liveSpeechIds.add(messageId);
        scheduleRefresh();
      } else if (
        ["conversation.changed", "workspace.changed"].includes(event.type)
      )
        scheduleRefresh();
      window.dispatchEvent(
        new CustomEvent("azur-run-event", { detail: event }),
      );
    }
  }
  function connect() {
    if (stopped) return;
    socket = new WebSocket(
      `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/runtime?after=${cursor}`,
    );
    socket.onopen = () => {
      online.value = true;
      backoff = 500;
      if (!workspace.value) scheduleRefresh();
    };
    socket.onmessage = (e) => {
      try {
        batch.push(JSON.parse(e.data));
        if (!frame) frame = requestAnimationFrame(flush);
      } catch {
        scheduleRefresh();
      }
    };
    socket.onclose = () => {
      online.value = false;
      if (!stopped)
        reconnect = window.setTimeout(
          connect,
          (backoff = Math.min(backoff * 2, 10000)),
        );
    };
    socket.onerror = () => socket?.close();
  }
  async function start() {
    try {
      await kernel.start();
      if (stopped) return;
      await refresh();
    } finally {
      connect();
    }
  }
  function stop() {
    stopped = true;
    socket?.close();
    clearTimeout(reconnect);
    clearTimeout(refreshTimer);
    cancelAnimationFrame(frame);
    batch = [];
    ++refreshGeneration;
    void kernel.stop();
  }
  async function inspect(id: string) {
    const detail = await api<WorkflowDetail>(`/api/runs/${id}`);
    if (!stopped) kernel.hydrateDetail(detail);
    return detail;
  }
  return {
    workspace,
    runs,
    online,
    error,
    streams,
    refresh,
    start,
    stop,
    inspect,
  };
}
