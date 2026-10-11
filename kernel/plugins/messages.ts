import type { Disposer } from "../events.ts";
import type { Plugin, PluginContext } from "../runtime.ts";
import type { RuntimeEnvelope } from "../runtime-events.ts";

export interface MessageStream {
  actorId: string;
  text: string;
  phase: string;
  messageId: string;
  createdAt: string;
  done: boolean;
  expression?: boolean;
  seq: number;
}

export interface PersistedMessage {
  id: string;
  body: string;
}

export interface MessageService {
  apply(event: RuntimeEnvelope): Promise<MessageStream | undefined>;
  prune(messages: PersistedMessage[]): void;
  subscribe(listener: (key: string) => void): Disposer;
  get(key: string): MessageStream | undefined;
  list(): Record<string, MessageStream>;
}

/** Projects streaming message events without coupling the chat UI to the journal format. */
export function messagePlugin(): Plugin {
  return {
    name: "messages",
    inject: ["runtime-events"],
    apply(ctx: PluginContext) {
      const streams = new Map<string, MessageStream>();
      const retired = new Map<string, number>();
      const removedRuns = new Map<string, number>();
      const listeners = new Set<(key: string) => void>();
      const notify = (key: string) => { for (const listener of listeners) listener(key); };
      const service: MessageService = {
        async apply(event) {
          if (event.type === "run.removed" && event.runId) {
            removedRuns.set(event.runId, Math.max(removedRuns.get(event.runId) ?? 0, event.seq));
            for (const key of [...streams.keys()]) {
              if (key.startsWith(event.runId + ":")) {
                streams.delete(key);
                notify(key);
              }
            }
            return;
          }
          if (event.type === "run.updated" && event.runId) {
            const status = event.payload.status;
            if (["completed", "failed", "cancelled", "paused"].includes(String(status))) {
              for (const [key, stream] of streams) {
                if (key.startsWith(event.runId + ":") && event.seq > stream.seq && !stream.done) {
                  stream.done = true;
                  stream.seq = event.seq;
                  notify(key);
                }
              }
            }
            return;
          }
          if (!event.runId || !["message.start", "message.delta", "message.complete"].includes(event.type)) return;
          if (removedRuns.has(event.runId)) return;
          const messageId = String(event.payload.messageId ?? (event.runId + ":" + (event.payload.assignmentId ?? "message")));
          const key = event.runId + ":" + messageId;
          const previous = streams.get(key);
          if (event.seq <= Math.max(previous?.seq ?? -1, retired.get(key) ?? -1)) return previous ? { ...previous } : undefined;
          const next: MessageStream = previous
            ? { ...previous }
            : {
                actorId: String(event.payload.actorId ?? ""),
                text: "",
                phase: String(event.payload.assignmentId ?? ""),
                messageId,
                createdAt: new Date(event.at * 1000).toISOString(),
                done: false,
                seq: event.seq,
              };
          next.seq = event.seq;
          if (event.type === "message.start") {
            next.text = "";
            next.done = false;
          } else if (event.type === "message.complete") {
            next.text = String(event.payload.text ?? next.text);
            next.done = true;
            next.expression = Boolean(event.payload.expression);
          } else {
            next.text += String(event.payload.delta ?? event.payload.text ?? "");
          }
          streams.set(key, next);
          notify(key);
          await ctx.events.emit("message/stream", { key, stream: { ...next }, source: event });
          return { ...next };
        },
        prune(messages) {
          const saved = new Map(messages.map(message => [message.id, message.body]));
          for (const [key, stream] of streams) {
            if (stream.done && saved.get(stream.messageId) === stream.text) {
              retired.set(key, stream.seq);
              streams.delete(key);
              notify(key);
            }
          }
        },
        subscribe(listener) {
          listeners.add(listener);
          return () => { listeners.delete(listener); };
        },
        get(key) {
          const stream = streams.get(key);
          return stream ? { ...stream } : undefined;
        },
        list() {
          return Object.fromEntries([...streams.entries()].map(([key, stream]) => [key, { ...stream }]));
        },
      };
      ctx.provide("messages", service);
      ctx.on<RuntimeEnvelope>("runtime/event", async event => { await service.apply(event); });
      ctx.effect(() => { streams.clear(); retired.clear(); removedRuns.clear(); listeners.clear(); });
    },
  };
}
