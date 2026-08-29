/**
 * A small typed pub/sub bus so new subsystems can observe what JARVIS is
 * doing without being wired into the Agent/Executor directly — matches the
 * event list from the architecture spec (jarvis.started, agent.tool_call,
 * application.opened, error, ...).
 */

export interface JarvisEventMap {
  "jarvis.started": Record<string, never>;
  "jarvis.stopped": Record<string, never>;
  "agent.started": { goal: string };
  "agent.thinking": { note?: string };
  "agent.tool_call": { tool: string; input: unknown; callId: string };
  "agent.tool_result": { tool: string; callId: string; success: boolean; result?: unknown; error?: unknown };
  "agent.finished": { goal: string; answer: string };
  "application.opened": { app: string };
  "application.closed": { app: string };
  "window.changed": { title: string };
  "error": { source: string; message: string; recoverable: boolean };
}

export type JarvisEventName = keyof JarvisEventMap;
type Listener<K extends JarvisEventName> = (payload: JarvisEventMap[K]) => void;

export class EventBus {
  private listeners: Map<JarvisEventName, Set<Listener<any>>> = new Map();

  on<K extends JarvisEventName>(event: K, listener: Listener<K>): () => void {
    let set = this.listeners.get(event);
    if (!set) {
      set = new Set();
      this.listeners.set(event, set);
    }
    set.add(listener);
    return () => set!.delete(listener);
  }

  emit<K extends JarvisEventName>(event: K, payload: JarvisEventMap[K]): void {
    const set = this.listeners.get(event);
    if (!set) return;
    for (const listener of set) {
      try {
        listener(payload);
      } catch (err) {
        // A subscriber's bug must never break the emitting call site.
        console.error(`[EventBus] listener for '${event}' threw:`, err);
      }
    }
  }

  listenerCount(event: JarvisEventName): number {
    return this.listeners.get(event)?.size ?? 0;
  }
}
