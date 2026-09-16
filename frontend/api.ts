import { reactive, ref, shallowRef } from "vue";
import type { Workspace, Run, RunEvent, ToolCall } from "./types";
import { liveSpeechIds } from './speechQueue';
import { preloadAssets } from "./assets";

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
  const runs = ref<Run[]>([]);
  const online = ref(false);
  const error = ref("");
  const streams = reactive<
    Record<string, { actorId: string; text: string; phase: string; messageId: string; createdAt: string; done: boolean }>
  >({});
  let cursor = 0,
    socket: WebSocket | undefined,
    reconnect = 0,
    stopped = false,
    refreshTimer = 0;
  let backoff = 500,
    batch: RunEvent[] = [],
    frame = 0;
  let initialized = false;
  let refreshGeneration = 0;
  async function refresh() {
    const generation = ++refreshGeneration;
    try {
      const [boot, active] = await Promise.all([
        api<{ workspace: Workspace }>("/api/bootstrap"),
        api<{ runs: Run[]; cursor: number }>("/api/runtime/state"),
      ]);
      if (generation !== refreshGeneration) return;
      workspace.value = boot.workspace;
      preloadAssets(boot.workspace.data.agents);
      const incomingIds = new Set(active.runs.map(r => r.id));
      // A snapshot started before a websocket event must not erase that new run.
      if (active.cursor >= cursor) runs.value = runs.value.filter(r => incomingIds.has(r.id));
      for (const run of active.runs) {
        const index = runs.value.findIndex((r) => r.id === run.id);
        if (index === -1) runs.value.push(run);
        else if (run.updatedAt >= runs.value[index].updatedAt)
          runs.value[index] = run;
      }
      runs.value.sort((a, b) => b.updatedAt - a.updatedAt);
      if (!initialized) {
        cursor = active.cursor;
        initialized = true;
      }
      const saved = Object.values(boot.workspace.data.messages).flat();
      for (const key in streams) {
        const stream = streams[key];
        if (stream.done && saved.some(m => m.id === stream.messageId && m.body === stream.text)) delete streams[key];
      }
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
    for (const event of batch) {
      if (event.type === "runtime.sync") {
        cursor = event.seq;
        scheduleRefresh();
        continue;
      }
      if (event.seq <= cursor) continue;
      if (cursor && event.seq !== cursor + 1) scheduleRefresh();
      cursor = event.seq;
      if (event.type === 'mind.changed') window.dispatchEvent(new CustomEvent('azur-mind-changed', {detail:event.payload}));
      if (event.type === "run.removed") {
        runs.value = runs.value.filter(r => r.id !== event.runId);
        for (const key in streams) if (key.startsWith(event.runId + ":")) delete streams[key];
      } else if (event.type === "run.updated" || event.type === "run.created") {
        const run = event.payload as unknown as Run;
        const i = runs.value.findIndex((r) => r.id === run.id);
        if (i === -1) runs.value.unshift(run);
        else runs.value[i] = run;
        if (
          ["completed", "failed", "cancelled", "paused"].includes(run.status)
        ) {
          for (const key in streams)
            if (key.startsWith(run.id + ":")) streams[key].done = true;
          scheduleRefresh();
        }
      } else if (["message.start", "message.delta", "message.complete"].includes(event.type)) {
        const p = event.payload;
        const messageId = String(p.messageId || event.runId + ":" + p.assignmentId);
        const key = event.runId + ":" + messageId;
        const entry = (streams[key] ||= {
          actorId: String(p.actorId),
          phase: String(p.assignmentId),
          text: "",
          messageId,
          createdAt: new Date(event.at * 1000).toISOString(),
          done: false,
        });
        if (event.type === "message.start") { entry.text = ""; entry.done = false; }
        else if (event.type === "message.complete") {
          if (p.expression) liveSpeechIds.add(messageId);
          entry.text = String(p.text || entry.text);
          entry.done = true;
          scheduleRefresh();
        } else entry.text += String(p.delta || p.text || "");
      } else if (
        ["conversation.changed", "workspace.changed"].includes(event.type)
      )
        scheduleRefresh();
      window.dispatchEvent(
        new CustomEvent("azur-run-event", { detail: event }),
      );
    }
    batch = [];
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
  }
  async function inspect(id: string) {
    return api<{ run: Run; calls: ToolCall[] }>(`/api/runs/${id}`);
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
