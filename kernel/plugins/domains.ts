import type { Plugin } from "../runtime.ts";
import type { Disposer } from "../events.ts";
import type { RuntimeEnvelope } from "../runtime-events.ts";

export type DomainKind = "mind" | "skill" | "social" | "workflow";

export interface DomainSignal {
  kind: DomainKind;
  key: string;
  actorId?: string;
  runId?: string | null;
  seq: number;
  type: string;
  payload: Record<string, unknown>;
}

export interface DomainService {
  apply(event: RuntimeEnvelope): Promise<DomainSignal | undefined>;
  subscribe(listener: (signal: DomainSignal) => void): Disposer;
  latest(kind: DomainKind, key?: string): DomainSignal | undefined;
  list(kind?: DomainKind): DomainSignal[];
}

/**
 * Small durable-domain projection shared by Vue and future native hosts.
 * Mind/social/skill events remain opaque payloads here; the plugin only owns
 * ordering, routing and subscriptions, so domain details stay in their
 * respective Python services until their native implementations arrive.
 */
export function domainPlugin(): Plugin {
  return {
    name: "domains",
    inject: ["runtime-events"],
    apply(ctx) {
      const signals = new Map<string, DomainSignal>();
      const listeners = new Set<(signal: DomainSignal) => void>();
      const sequence = new Map<string, number>();
      const classify = (event: RuntimeEnvelope): DomainSignal | undefined => {
        let kind: DomainKind | undefined;
        if (event.type.startsWith("mind.")) kind = "mind";
        else if (event.type.startsWith("skill.")) kind = "skill";
        else if (event.type.startsWith("social.")) kind = "social";
        else if (event.type.startsWith("workflow.")) kind = "workflow";
        if (!kind) return undefined;
        const actorId = typeof event.payload.actorId === "string" ? event.payload.actorId : undefined;
        const key = `${kind}:${actorId ?? event.runId ?? "global"}`;
        return { kind, key, actorId, runId: event.runId, seq: event.seq, type: event.type, payload: structuredClone(event.payload) };
      };
      const service: DomainService = {
        async apply(event) {
          const signal = classify(event);
          if (!signal || signal.seq <= (sequence.get(signal.key) ?? -1)) return undefined;
          sequence.set(signal.key, signal.seq);
          signals.set(signal.key, signal);
          for (const listener of listeners) listener(structuredClone(signal));
          await ctx.events.emit(`domain/${signal.kind}`, structuredClone(signal));
          return structuredClone(signal);
        },
        subscribe(listener) {
          listeners.add(listener);
          return () => listeners.delete(listener);
        },
        latest(kind, key) {
          const signal = key ? signals.get(`${kind}:${key}`) : [...signals.values()].reverse().find(item => item.kind === kind);
          return signal ? structuredClone(signal) : undefined;
        },
        list(kind) {
          return [...signals.values()].filter(item => !kind || item.kind === kind).map(item => structuredClone(item));
        },
      };
      ctx.provide("domains", service);
      ctx.on<RuntimeEnvelope>("runtime/event", async event => { await service.apply(event); });
      ctx.effect(() => { signals.clear(); sequence.clear(); listeners.clear(); });
    },
  };
}
