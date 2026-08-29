import { describe, it, expect, vi } from "vitest";
import { z } from "zod";

import { ToolRegistry, type JarvisTool } from "../src/ToolRegistry.ts";
import { PermissionManager } from "../src/PermissionManager.ts";
import { EventBus } from "../src/EventBus.ts";

function makeTool(overrides: Partial<JarvisTool> = {}): JarvisTool {
  return {
    name: "echo",
    description: "Echoes input back",
    inputSchema: { type: "object", properties: { text: { type: "string" } }, required: ["text"] },
    zodSchema: z.object({ text: z.string() }),
    level: "SAFE",
    execute: async (input: unknown) => input,
    ...overrides,
  };
}

describe("ToolRegistry", () => {
  it("registers and lists tools, exposing Claude-shaped schemas", () => {
    const registry = new ToolRegistry(new PermissionManager());
    registry.register(makeTool());
    const described = registry.describeForClaude();
    expect(described).toEqual([
      { name: "echo", description: "Echoes input back", input_schema: makeTool().inputSchema },
    ]);
  });

  it("refuses a duplicate tool name", () => {
    const registry = new ToolRegistry(new PermissionManager());
    registry.register(makeTool());
    expect(() => registry.register(makeTool())).toThrow(/already registered/);
  });

  it("invoking an unknown tool returns a structured error, not a throw", async () => {
    const registry = new ToolRegistry(new PermissionManager());
    const outcome = await registry.invoke("nope", {});
    expect(outcome.success).toBe(false);
    expect(outcome.error?.code).toBe("UNKNOWN_TOOL");
  });

  it("rejects input that fails the zod schema before execute() ever runs", async () => {
    const registry = new ToolRegistry(new PermissionManager());
    const execute = vi.fn(async (input: unknown) => input);
    registry.register(makeTool({ execute }));
    const outcome = await registry.invoke("echo", { text: 42 }); // wrong type
    expect(outcome.success).toBe(false);
    expect(outcome.error?.code).toBe("INVALID_INPUT");
    expect(execute).not.toHaveBeenCalled();
  });

  it("a DANGEROUS tool is refused without confirmation and never executes", async () => {
    const registry = new ToolRegistry(new PermissionManager());
    const execute = vi.fn(async (input: unknown) => input);
    registry.register(makeTool({ level: "DANGEROUS", execute }));
    const outcome = await registry.invoke("echo", { text: "hi" });
    expect(outcome.success).toBe(false);
    expect(outcome.error?.code).toBe("PERMISSION_DENIED");
    expect(execute).not.toHaveBeenCalled();
  });

  it("a DANGEROUS tool runs once confirmed:true is passed", async () => {
    const registry = new ToolRegistry(new PermissionManager());
    registry.register(makeTool({ level: "DANGEROUS" }));
    const outcome = await registry.invoke("echo", { text: "hi" }, { confirmed: true });
    expect(outcome.success).toBe(true);
    expect(outcome.result).toEqual({ text: "hi" });
  });

  it("a successful call emits agent.tool_call and agent.tool_result on the EventBus", async () => {
    const events = new EventBus();
    const calls: string[] = [];
    events.on("agent.tool_call", () => calls.push("call"));
    events.on("agent.tool_result", () => calls.push("result"));
    const registry = new ToolRegistry(new PermissionManager(), events);
    registry.register(makeTool());
    await registry.invoke("echo", { text: "hi" });
    expect(calls).toEqual(["call", "result"]);
  });

  it("an exception thrown inside execute() is caught and returned as a structured error", async () => {
    const registry = new ToolRegistry(new PermissionManager());
    registry.register(
      makeTool({
        execute: async () => {
          throw new Error("native-core exploded");
        },
      }),
    );
    const outcome = await registry.invoke("echo", { text: "hi" });
    expect(outcome.success).toBe(false);
    expect(outcome.error?.message).toContain("native-core exploded");
  });
});
