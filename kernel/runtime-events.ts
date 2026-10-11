import type { Disposer } from "./events.ts";
import type { Plugin, PluginContext } from "./runtime.ts";

export interface RuntimeEnvelope {
  seq: number;
  runId: string | null;
  type: string;
  payload: Record<string, unknown>;
  at: number;
}

export interface RuntimeEventSource {
  read(after: number, runId?: string): Promise<RuntimeEnvelope[]>;
}

export interface HttpRuntimeEventSourceOptions {
  endpoint: string;
  fetchImpl?: typeof fetch;
  headers?: HeadersInit;
  signal?: AbortSignal;
}

/** Adapter for the existing Python RunStore event endpoint. */
export class HttpRuntimeEventSource implements RuntimeEventSource {
  private readonly endpoint: string;
  private readonly fetchImpl: typeof fetch;
  private readonly headers?: HeadersInit;
  private readonly signal?: AbortSignal;

  constructor(options: HttpRuntimeEventSourceOptions) {
    this.endpoint = options.endpoint;
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.headers = options.headers;
    this.signal = options.signal;
  }

  async read(after: number, runId?: string): Promise<RuntimeEnvelope[]> {
    const url = new URL(this.endpoint);
    url.searchParams.set("after", String(Math.max(0, after)));
    if (runId) url.searchParams.set("runId", runId);
    const response = await this.fetchImpl(url, { headers: this.headers, signal: this.signal });
    if (!response.ok) throw new Error(`Runtime event request failed: ${response.status}`);
    const body = await response.json() as { events?: unknown };
    if (!Array.isArray(body.events)) return [];
    return body.events.map(normalizeEnvelope);
  }
}

export interface RuntimeEventService {
  readonly cursor: number;
  sync(): Promise<RuntimeEnvelope[]>;
  ingest(event: RuntimeEnvelope): Promise<boolean>;
}

export interface RuntimeEventsPluginOptions {
  source: RuntimeEventSource;
  startAt?: number;
}

/** Publishes RunStore events onto the kernel bus without duplicating storage. */
export function runtimeEventsPlugin(options: RuntimeEventsPluginOptions): Plugin {
  return {
    name: "runtime-events",
    apply(ctx: PluginContext) {
      let cursor = Math.max(0, options.startAt ?? 0);
      const service: RuntimeEventService = {
        get cursor() { return cursor; },
        async sync() {
          const events = await options.source.read(cursor);
          for (const event of events) {
            await service.ingest(event);
          }
          return events;
        },
        async ingest(event) {
          if (event.seq <= cursor) return false;
          cursor = event.seq;
          await ctx.events.emit("runtime/event", event);
          return true;
        },
      };
      ctx.provide("runtime-events", service);
      ctx.effect(() => { cursor = Math.max(0, options.startAt ?? 0); });
    },
  };
}

function normalizeEnvelope(value: unknown): RuntimeEnvelope {
  if (!value || typeof value !== "object") throw new Error("Invalid runtime event envelope");
  const item = value as Record<string, unknown>;
  const seq = Number(item.seq);
  if (!Number.isFinite(seq) || seq < 1) throw new Error("Invalid runtime event sequence");
  const payload = item.payload;
  return {
    seq,
    runId: typeof item.runId === "string" ? item.runId : null,
    type: typeof item.type === "string" ? item.type : "runtime.unknown",
    payload: payload && typeof payload === "object" ? payload as Record<string, unknown> : {},
    at: Number(item.at ?? Date.now()),
  };
}

export type RuntimeEventDisposer = Disposer;
