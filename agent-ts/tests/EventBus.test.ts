import { describe, it, expect, vi } from "vitest";
import { EventBus } from "../src/EventBus.ts";

describe("EventBus", () => {
  it("delivers a payload to a subscribed listener", () => {
    const bus = new EventBus();
    const listener = vi.fn();
    bus.on("agent.started", listener);
    bus.emit("agent.started", { goal: "test goal" });
    expect(listener).toHaveBeenCalledWith({ goal: "test goal" });
  });

  it("unsubscribe stops further delivery", () => {
    const bus = new EventBus();
    const listener = vi.fn();
    const unsubscribe = bus.on("jarvis.started", listener);
    unsubscribe();
    bus.emit("jarvis.started", {});
    expect(listener).not.toHaveBeenCalled();
  });

  it("a throwing listener does not break emit for other listeners", () => {
    const bus = new EventBus();
    const good = vi.fn();
    bus.on("error", () => {
      throw new Error("boom");
    });
    bus.on("error", good);
    expect(() => bus.emit("error", { source: "x", message: "y", recoverable: true })).not.toThrow();
    expect(good).toHaveBeenCalled();
  });

  it("listenerCount reflects subscriptions", () => {
    const bus = new EventBus();
    expect(bus.listenerCount("jarvis.stopped")).toBe(0);
    bus.on("jarvis.stopped", () => {});
    expect(bus.listenerCount("jarvis.stopped")).toBe(1);
  });
});
