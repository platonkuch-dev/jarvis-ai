import { describe, it, expect, vi } from "vitest";
import { z } from "zod";
import type Anthropic from "@anthropic-ai/sdk";

import { Agent } from "../src/Agent.ts";
import { ToolRegistry } from "../src/ToolRegistry.ts";
import { PermissionManager } from "../src/PermissionManager.ts";
import { JarvisError } from "../src/protocol.ts";

function textMessage(text: string): Anthropic.Message {
  return {
    id: "msg_1", type: "message", role: "assistant", model: "test",
    content: [{ type: "text", text, citations: null }],
    stop_reason: "end_turn", stop_sequence: null,
    usage: { input_tokens: 1, output_tokens: 1 } as any,
  } as unknown as Anthropic.Message;
}

function toolUseMessage(name: string, input: unknown, id = "tool_1"): Anthropic.Message {
  return {
    id: "msg_2", type: "message", role: "assistant", model: "test",
    content: [{ type: "tool_use", id, name, input }],
    stop_reason: "tool_use", stop_sequence: null,
    usage: { input_tokens: 1, output_tokens: 1 } as any,
  } as unknown as Anthropic.Message;
}

function makeRegistry(execute: (input: unknown) => Promise<unknown>) {
  const registry = new ToolRegistry(new PermissionManager());
  registry.register({
    name: "list_windows",
    description: "lists windows",
    inputSchema: { type: "object", properties: {} },
    zodSchema: z.object({}),
    level: "SAFE",
    execute,
  });
  return registry;
}

describe("Agent", () => {
  it("returns Claude's text answer directly when no tool is called (single turn)", async () => {
    const create = vi.fn().mockResolvedValue(textMessage("The weather is fine."));
    const client = { messages: { create } } as unknown as Anthropic;
    const registry = makeRegistry(async () => ({}));
    const agent = new Agent(client, registry);

    const answer = await agent.run("what's the weather");
    expect(answer).toBe("The weather is fine.");
    expect(create).toHaveBeenCalledTimes(1);
  });

  it("executes a tool call, feeds the result back, and returns the follow-up answer (Act -> Observe -> Finish)", async () => {
    const create = vi
      .fn()
      .mockResolvedValueOnce(toolUseMessage("list_windows", {}))
      .mockResolvedValueOnce(textMessage("You have 3 windows open."));
    const client = { messages: { create } } as unknown as Anthropic;
    const execute = vi.fn().mockResolvedValue({ windows: ["a", "b", "c"] });
    const registry = makeRegistry(execute);
    const agent = new Agent(client, registry);

    const answer = await agent.run("how many windows are open?");

    expect(answer).toBe("You have 3 windows open.");
    expect(execute).toHaveBeenCalledTimes(1);
    expect(create).toHaveBeenCalledTimes(2);

    // The second call must include the tool_result fed back from the first.
    const secondCallArgs = create.mock.calls[1]![0];
    const lastMessage = secondCallArgs.messages[secondCallArgs.messages.length - 1];
    expect(lastMessage.role).toBe("user");
    expect(lastMessage.content[0].type).toBe("tool_result");
  });

  it("a failed tool call is still fed back (as an error) so Claude can correct course", async () => {
    const create = vi
      .fn()
      .mockResolvedValueOnce(toolUseMessage("list_windows", {}))
      .mockResolvedValueOnce(textMessage("That didn't work, here's what I know instead."));
    const client = { messages: { create } } as unknown as Anthropic;
    const registry = makeRegistry(async () => {
      throw new Error("native-core unreachable");
    });
    const agent = new Agent(client, registry);

    const answer = await agent.run("list windows");
    expect(answer).toBe("That didn't work, here's what I know instead.");

    const secondCallArgs = create.mock.calls[1]![0];
    const toolResultBlock = secondCallArgs.messages[secondCallArgs.messages.length - 1].content[0];
    expect(toolResultBlock.is_error).toBe(true);
  });

  it("throws AGENT_MAX_TURNS_EXCEEDED if Claude never stops calling tools", async () => {
    const create = vi.fn().mockResolvedValue(toolUseMessage("list_windows", {}));
    const client = { messages: { create } } as unknown as Anthropic;
    const registry = makeRegistry(async () => ({}));
    const agent = new Agent(client, registry, { maxTurns: 3 });

    await expect(agent.run("infinite loop")).rejects.toMatchObject({ code: "AGENT_MAX_TURNS_EXCEEDED" });
    expect(create).toHaveBeenCalledTimes(3);
  });

  it("rejects with a JarvisError instance specifically", async () => {
    const create = vi.fn().mockResolvedValue(toolUseMessage("list_windows", {}));
    const client = { messages: { create } } as unknown as Anthropic;
    const registry = makeRegistry(async () => ({}));
    const agent = new Agent(client, registry, { maxTurns: 1 });

    await expect(agent.run("x")).rejects.toBeInstanceOf(JarvisError);
  });
});
