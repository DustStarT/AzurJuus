import { computed, reactive, shallowRef } from "vue";
import type { InjectionKey } from "vue";
import { KernelHost } from "../../kernel/host.ts";
import { HttpRuntimeEventSource } from "../../kernel/runtime-events.ts";
import type { RuntimeEnvelope, RuntimeEventService } from "../../kernel/runtime-events.ts";
import type { WorkflowService, WorkflowSnapshot, WorkflowCall } from "../../kernel/plugins/workflows.ts";
import type { Run } from "./types";
import type { MessageService, PersistedMessage, MessageStream } from "../../kernel/plugins/messages.ts";
import type { DomainService, DomainSignal, DomainKind } from "../../kernel/plugins/domains.ts";

export interface WorkflowDetail {
  run: Run;
  calls: WorkflowCall[];
  cursor: number;
}

/** Each workspace owns its host and Vue projections; no process-wide browser singleton. */
export function createKernelRuntime() {
  const state = shallowRef<Record<string, WorkflowSnapshot>>({});
  const streams = reactive<Record<string, MessageStream>>({});
  let host: KernelHost | undefined;
  let workflows: WorkflowService | undefined;
  let messages: MessageService | undefined;
  let domains: DomainService | undefined;
  let unsubscribe: (() => void) | undefined;
  let unsubscribeMessages: (() => void) | undefined;
  let generation = 0;
  const runs = computed(() => Object.values(state.value)
    .filter(item => typeof item.data.id === "string")
    .map(item => item.data as unknown as Run)
    .sort((a, b) => b.updatedAt - a.updatedAt));

  async function start() {
    const version = ++generation;
    const next = new KernelHost({
      runtimeSource: new HttpRuntimeEventSource({
        endpoint: new URL("/api/runtime/events", window.location.origin).toString(),
      }),
    });
    await next.start();
    if (version !== generation) { await next.stop(); return; }
    host = next;
    workflows = host.kernel.getService<WorkflowService>("workflows")!;
    messages = host.kernel.getService<MessageService>("messages")!;
    domains = host.kernel.getService<DomainService>("domains")!;
    unsubscribe = workflows.subscribe(id => {
      const snapshot = workflows!.get(id);
      const records = { ...state.value };
      if (snapshot) records[id] = snapshot;
      else delete records[id];
      state.value = records;
    });
    unsubscribeMessages = messages.subscribe(key => {
      const stream = messages!.get(key);
      if (stream) streams[key] = stream;
      else delete streams[key];
    });
  }

  function hydrateRuns(records: Run[], cursor: number, replace = true) {
    workflows?.hydrate(records as unknown as Array<Record<string, unknown>>, cursor, replace);
  }

  function hydrateDetail(detail: WorkflowDetail) {
    hydrateRuns([detail.run], detail.cursor, false);
    workflows?.hydrateCalls(detail.run.id, detail.calls, detail.cursor);
  }

  function pruneMessages(records: PersistedMessage[]) {
    messages?.prune(records);
  }

  function subscribeDomain(listener: (signal: DomainSignal) => void): (() => void) {
    return domains?.subscribe(listener) ?? (() => undefined);
  }

  function domainSignals(kind?: DomainKind) {
    return domains?.list(kind) ?? [];
  }

  async function ingest(event: RuntimeEnvelope) {
    await host?.kernel.getService<RuntimeEventService>("runtime-events")?.ingest(event);
  }

  async function stop() {
    ++generation;
    unsubscribe?.();
    unsubscribe = undefined;
    unsubscribeMessages?.();
    unsubscribeMessages = undefined;
    workflows = undefined;
    messages = undefined;
    domains = undefined;
    const previous = host;
    host = undefined;
    state.value = {};
    for (const key of Object.keys(streams)) delete streams[key];
    await previous?.stop();
  }

  return { state, streams, runs, start, stop, ingest, hydrateRuns, hydrateDetail, pruneMessages, subscribeDomain, domainSignals };
}

export type KernelRuntime = ReturnType<typeof createKernelRuntime>;
export const kernelRuntimeKey: InjectionKey<KernelRuntime> = Symbol("azurjuus-kernel-runtime");
