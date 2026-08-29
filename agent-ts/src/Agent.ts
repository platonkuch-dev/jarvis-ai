/**
 * The reasoning engine: Claude is the "brain", but it never touches
 * Windows directly — every action goes through ToolRegistry, which enforces
 * schema validation and PermissionManager before anything reaches
 * rustBridge. Implements the loop the architecture spec asks for:
 *
 *   Goal -> Reason -> Plan -> Act -> Observe -> Correct -> Finish
 *
 * concretely: each turn, Claude either produces a final text answer (Finish)
 * or one-or-more tool_use blocks (Act); every tool result is fed back
 * (Observe) so the next turn can adjust course (Correct) instead of blindly
 * continuing a broken plan.
 */
import Anthropic from "@anthropic-ai/sdk";

import type { ToolRegistry } from "./ToolRegistry.ts";
import type { EventBus } from "./EventBus.ts";
import { JarvisError } from "./protocol.ts";

const DEFAULT_SYSTEM_PROMPT = `You are JARVIS, an autonomous Windows computer-control agent.
You have tools to launch/close/focus/move/resize application windows, list windows and
processes, and find/click/type into/read UI controls inside application windows via
Windows UI Automation.

Work autonomously: for a multi-step request, call tools one at a time, look at each
result, and adjust — don't ask the user to narrate every step, and don't assume a step
worked without evidence in the tool result. Prefer find_ui_element/get_ui_tree before
click_element/type_into_element when you don't already know a control exists. Use
wait_for_window/wait_for_ui_element instead of guessing that something is ready yet.
When you are done, or when a tool result shows the task cannot proceed, respond with a
short, clear final answer in plain text (no tool call) — that ends the turn.`;

export interface AgentOptions {
  model?: string;
  maxTurns?: number;
  systemPrompt?: string;
  maxTokens?: number;
}

export class Agent {
  private readonly client: Anthropic;
  private readonly tools: ToolRegistry;
  private readonly events: EventBus | undefined;
  private readonly model: string;
  private readonly maxTurns: number;
  private readonly maxTokens: number;
  private readonly systemPrompt: string;

  constructor(client: Anthropic, tools: ToolRegistry, options?: AgentOptions, events?: EventBus) {
    this.client = client;
    this.tools = tools;
    this.model = options?.model ?? "claude-sonnet-5";
    this.maxTurns = options?.maxTurns ?? 12;
    this.maxTokens = options?.maxTokens ?? 2048;
    this.systemPrompt = options?.systemPrompt ?? DEFAULT_SYSTEM_PROMPT;
    this.events = events;
  }

  private claudeTools(): Anthropic.Tool[] {
    return this.tools.describeForClaude().map((t) => ({
      name: t.name,
      description: t.description,
      input_schema: t.input_schema as Anthropic.Tool.InputSchema,
    }));
  }

  async run(goal: string): Promise<string> {
    this.events?.emit("agent.started", { goal });
    // Rebuilt (never mutated in place) each turn — a shared, in-place-pushed
    // array would let a caller inspecting a captured `create()` call's
    // `messages` argument see later turns' mutations bleed into it, since
    // that argument is the same array reference every turn.
    let messages: Anthropic.MessageParam[] = [{ role: "user", content: goal }];
    const claudeTools = this.claudeTools();

    for (let turn = 0; turn < this.maxTurns; turn++) {
      this.events?.emit("agent.thinking", { note: `turn ${turn + 1}/${this.maxTurns}` });

      const response = await this.client.messages.create({
        model: this.model,
        max_tokens: this.maxTokens,
        system: this.systemPrompt,
        tools: claudeTools,
        messages,
      });

      messages = [...messages, { role: "assistant", content: response.content }];

      const toolUses = response.content.filter(
        (block): block is Anthropic.ToolUseBlock => block.type === "tool_use",
      );

      if (toolUses.length === 0) {
        const finalText = response.content
          .filter((block): block is Anthropic.TextBlock => block.type === "text")
          .map((block) => block.text)
          .join("\n")
          .trim();
        this.events?.emit("agent.finished", { goal, answer: finalText });
        return finalText;
      }

      const toolResults: Anthropic.ToolResultBlockParam[] = [];
      for (const use of toolUses) {
        const outcome = await this.tools.invoke(use.name, use.input, { callId: use.id });
        toolResults.push({
          type: "tool_result",
          tool_use_id: use.id,
          content: JSON.stringify(outcome.success ? (outcome.result ?? {}) : { error: outcome.error }),
          is_error: !outcome.success,
        });
      }
      messages = [...messages, { role: "user", content: toolResults }];
    }

    throw new JarvisError({
      code: "AGENT_MAX_TURNS_EXCEEDED",
      message: `Did not reach a final answer for goal "${goal}" within ${this.maxTurns} turns.`,
      source: "Agent",
      recoverable: true,
    });
  }
}
