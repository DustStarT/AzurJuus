import { Context, type Fiber } from "@deepseek-ai/cordis";
import type { Disposer, EventListener } from "./events.ts";

declare module "@deepseek-ai/cordis" {
  interface Events {
    [event: string]: (...args: any[]) => any;
  }
}

export interface KernelEvents {
  on<T>(event: string, listener: EventListener<T>): Disposer;
  emit<T>(event: string, payload: T): Promise<void>;
}

export interface PluginContext {
  readonly native: Context;
  readonly events: KernelEvents;
  provide<T>(key: string, service: T): Disposer;
  get<T>(key: string): T | undefined;
  require<T>(key: string): T;
  effect(disposer: Disposer): Disposer;
  on<T>(event: string, listener: EventListener<T>): Disposer;
}

export interface Plugin {
  readonly name: string;
  readonly inject?: readonly string[];
  readonly apply: (ctx: PluginContext) => void | Promise<void>;
}

/** Application contracts hosted by the official DeepSeek Cordis Context. */
export class PluginKernel {
  readonly native = new Context();
  readonly events: KernelEvents = {
    on: <T>(event: string, listener: EventListener<T>) => this.native.on(event, listener),
    emit: <T>(event: string, payload: T) => this.native.parallel(event, payload),
  };
  private readonly plugins: Plugin[] = [];
  private readonly fibers: Fiber[] = [];
  private started = false;

  use(plugin: Plugin): this {
    if (this.started || this.fibers.length) throw new Error("Register plugins before start");
    if (this.plugins.some(item => item.name === plugin.name)) throw new Error(`Plugin already registered: ${plugin.name}`);
    this.plugins.push(plugin);
    return this;
  }

  hasService(key: string): boolean { return this.native.get(key) !== undefined; }
  getService<T>(key: string): T | undefined { return this.native.get(key) as T | undefined; }

  async start(): Promise<void> {
    if (this.started) return;
    const pending = [...this.plugins];
    try {
      while (pending.length) {
        const index = pending.findIndex(plugin => (plugin.inject ?? []).every(key => this.hasService(key)));
        if (index < 0) throw new Error(`Unresolved plugin dependencies: ${pending.map(item => item.name).join(", ")}`);
        const plugin = pending.splice(index, 1)[0];
        const fiber = this.native.plugin({
          name: plugin.name,
          inject: [...(plugin.inject ?? [])],
          apply: async (native: Context) => {
            const events: KernelEvents = {
              on: <T>(event: string, listener: EventListener<T>) => native.on(event, listener),
              emit: <T>(event: string, payload: T) => native.parallel(event, payload),
            };
            await plugin.apply({
              native, events,
              provide: <T>(key: string, service: T) => native.provide(key, service),
              get: <T>(key: string) => native.get(key) as T | undefined,
              require: <T>(key: string) => {
                const value = native.get(key) as T | undefined;
                if (value === undefined) throw new Error(`Required service is unavailable: ${key}`);
                return value;
              },
              effect: (dispose: Disposer) => native.effect(() => dispose),
              on: events.on,
            });
          },
        });
        this.fibers.push(fiber);
        await fiber.await();
        await this.events.emit("runtime/plugin-loaded", { name: plugin.name });
      }
      this.started = true;
      await this.events.emit("runtime/started", { plugins: this.plugins.map(item => item.name) });
    } catch (error) {
      await this.stop();
      throw error;
    }
  }

  async stop(): Promise<void> {
    for (const fiber of this.fibers.splice(0).reverse()) {
      await fiber.dispose();
      await this.events.emit("runtime/plugin-unloaded", { name: fiber.name });
    }
    this.started = false;
  }
}
