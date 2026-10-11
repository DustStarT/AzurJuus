import type { Plugin } from "../runtime.ts";
import type { Disposer } from "../events.ts";
import type { RuntimeEnvelope } from "../runtime-events.ts";

export interface WorkflowCall {
  id: string;
  run_id: string;
  name: string;
  args: Record<string, unknown>;
  status: string;
  result?: Record<string, unknown>;
  approval?: string;
  phase?: string;
}

export interface WorkflowSnapshot {
  id: string;
  status?: string;
  updatedAt: number;
  data: Record<string, unknown>;
  calls: Record<string, WorkflowCall>;
  outputs: Record<string, string>;
  lastEvent: RuntimeEnvelope;
}

export interface WorkflowService {
  apply(event: RuntimeEnvelope): Promise<WorkflowSnapshot | undefined>;
  hydrate(records: Array<Record<string, unknown>>, cursor?: number, replace?: boolean): void;
  hydrateCalls(id: string, calls: WorkflowCall[], cursor: number): void;
  subscribe(listener: (id: string) => void): Disposer;
  get(id: string): WorkflowSnapshot | undefined;
  list(): WorkflowSnapshot[];
}

/** Run data, tool calls and output are separate projections of the same journal. */
export function workflowPlugin(): Plugin {
  return {
    name: "workflows",
    inject: ["runtime-events"],
    apply(ctx) {
      const workflows = new Map<string, WorkflowSnapshot>();
      const runVersions = new Map<string, number>();
      const callVersions = new Map<string, Map<string, number>>();
      const callWatermarks = new Map<string, number>();
      const removed = new Map<string, number>();
      const listeners = new Set<(id: string) => void>();
      const notify = (id: string) => { for (const listener of listeners) listener(id); };
      const create = (id: string, event: RuntimeEnvelope): WorkflowSnapshot => ({
        id, updatedAt: event.at, data: {}, calls: {}, outputs: {}, lastEvent: structuredClone(event),
      });
      const service: WorkflowService = {
        async apply(event) {
          if (!event.runId) return;
          const id = event.runId;
          if (event.type === "run.removed") {
            if (event.seq <= Math.max(removed.get(id) ?? -1, runVersions.get(id) ?? -1)) return;
            removed.set(id, event.seq);
            workflows.delete(id);
            callVersions.delete(id);
            callWatermarks.delete(id);
            notify(id);
            await ctx.events.emit("workflow/removed", { workflowId: id, source: event });
            return;
          }
          if (event.seq <= (removed.get(id) ?? -1)) return;
          const isRun = event.type === "run.created" || event.type === "run.updated";
          const isOutput = event.type === "tool.output";
          const isCall = event.type.startsWith("tool.") && !isOutput;
          if (!isRun && !isCall && !isOutput) return;
          const next = workflows.get(id) ?? create(id, event);
          if (isRun) {
            if (event.seq <= (runVersions.get(id) ?? -1)) return;
            next.data = structuredClone(event.payload);
            next.status = typeof event.payload.status === "string" ? event.payload.status : undefined;
            next.updatedAt = Number(event.payload.updatedAt ?? event.at);
            runVersions.set(id, event.seq);
          } else {
            const callId = typeof event.payload.callId === "string" ? event.payload.callId : undefined;
            if (!callId) return;
            if (isOutput) {
              next.outputs[callId] = ((next.outputs[callId] ?? "") + String(event.payload.text ?? "")).slice(-24000);
            } else {
              const versions = callVersions.get(id) ?? new Map<string, number>();
              if (event.seq <= Math.max(versions.get(callId) ?? -1, callWatermarks.get(id) ?? -1)) return;
              const payload = structuredClone(event.payload);
              next.calls[callId] = {
                ...next.calls[callId],
                id: callId, run_id: id,
                name: String(payload.name ?? next.calls[callId]?.name ?? ""),
                args: (payload.args ?? next.calls[callId]?.args ?? {}) as Record<string, unknown>,
                status: event.type.slice("tool.".length),
                result: payload.result as WorkflowCall["result"],
                phase: payload.assignmentId as WorkflowCall["phase"],
              };
              versions.set(callId, event.seq);
              callVersions.set(id, versions);
            }
          }
          next.lastEvent = structuredClone(event);
          workflows.set(id, next);
          notify(id);
          await ctx.events.emit(isRun ? (event.type === "run.created" ? "workflow/created" : "workflow/updated") : "workflow/tool",
            { workflow: structuredClone(next), source: event });
          return structuredClone(next);
        },
        hydrate(records, cursor = 0, replace = false) {
          const ids = new Set(records.map(record => record.id));
          if (replace) {
            for (const [id] of workflows) {
              if (!ids.has(id) && (runVersions.get(id) ?? -1) <= cursor) {
                workflows.delete(id);
                removed.set(id, cursor);
                callVersions.delete(id);
                callWatermarks.delete(id);
                notify(id);
              }
            }
          }
          for (const record of records) {
            if (typeof record.id !== "string") continue;
            const id = record.id;
            if (cursor < (runVersions.get(id) ?? -1) || cursor <= (removed.get(id) ?? -1)) continue;
            const data = structuredClone(record);
            const event: RuntimeEnvelope = { seq: cursor, runId: id, type: "run.hydrated", payload: data, at: Number(record.updatedAt ?? 0) };
            const next = workflows.get(id) ?? create(id, event);
            next.data = data;
            next.status = typeof record.status === "string" ? record.status : undefined;
            next.updatedAt = event.at;
            runVersions.set(id, cursor);
            workflows.set(id, next);
            notify(id);
          }
        },
        hydrateCalls(id, calls, cursor) {
          if (cursor <= (removed.get(id) ?? -1) || cursor < (callWatermarks.get(id) ?? -1)) return;
          const event: RuntimeEnvelope = { seq: cursor, runId: id, type: "tool.hydrated", payload: {}, at: 0 };
          const next = workflows.get(id) ?? create(id, event);
          const versions = callVersions.get(id) ?? new Map<string, number>();
          const merged = Object.fromEntries(calls.map(call => [call.id, structuredClone(call)]));
          for (const [callId, call] of Object.entries(next.calls)) {
            if ((versions.get(callId) ?? -1) > cursor) merged[callId] = call;
          }
          next.calls = merged;
          callWatermarks.set(id, cursor);
          workflows.set(id, next);
          notify(id);
        },
        subscribe(listener) {
          listeners.add(listener);
          return () => { listeners.delete(listener); };
        },
        get(id) {
          const item = workflows.get(id);
          return item ? structuredClone(item) : undefined;
        },
        list() {
          return [...workflows.values()].map(item => structuredClone(item));
        },
      };
      ctx.provide("workflows", service);
      ctx.on<RuntimeEnvelope>("runtime/event", async event => { await service.apply(event); });
      ctx.effect(() => {
        workflows.clear(); runVersions.clear(); callVersions.clear();
        callWatermarks.clear(); removed.clear(); listeners.clear();
      });
    },
  };
}
