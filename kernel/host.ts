import { PluginKernel } from "./runtime.ts";
import { runtimeEventsPlugin, type RuntimeEventService, type RuntimeEventSource } from "./runtime-events.ts";
import { workflowPlugin } from "./plugins/workflows.ts";
import { messagePlugin } from "./plugins/messages.ts";
import { domainPlugin } from "./plugins/domains.ts";

export interface KernelHostOptions {
  runtimeSource: RuntimeEventSource;
  startAt?: number;
}

/**
 * Browser and Node composition root for the official Cordis application host.
 * Domain plugins consume journal events independently of the transport.
 */
export class KernelHost {
  readonly kernel: PluginKernel;
  private started = false;

  constructor(options: KernelHostOptions) {
    this.kernel = new PluginKernel()
      .use(runtimeEventsPlugin({ source: options.runtimeSource, startAt: options.startAt }))
      .use(workflowPlugin())
      .use(messagePlugin())
      .use(domainPlugin());
  }

  async start(): Promise<void> {
    if (this.started) return;
    await this.kernel.start();
    this.started = true;
  }

  async syncRuntime() {
    if (!this.started) throw new Error("Kernel host has not started");
    return this.kernel.getService<RuntimeEventService>("runtime-events")!.sync();
  }

  async stop(): Promise<void> {
    if (!this.started) return;
    await this.kernel.stop();
    this.started = false;
  }
}
