/**
 * The single place tools get registered and looked up. Claude only ever
 * sees the `describe()` output of this registry (name/description/JSON
 * schema) — never a live reference to rustBridge, a file handle, or
 * anything else it could reach outside a named, schema-validated tool call.
 */
import type { ZodType } from "zod";

import { JarvisError } from "./protocol.ts";
import { PermissionManager, type PermissionLevel } from "./PermissionManager.ts";
import type { EventBus } from "./EventBus.ts";

/** Matches the architecture spec's JarvisTool interface, plus the fields
 * PermissionManager and runtime validation need. */
export interface JarvisTool {
  name: string;
  description: string;
  /** JSON Schema, handed to Claude verbatim as the tool's input_schema. */
  inputSchema: Record<string, unknown>;
  /** Runtime validator for the same shape — belt-and-suspenders against a
   * model call that doesn't actually conform to the declared schema. */
  zodSchema: ZodType;
  level: PermissionLevel;
  execute(input: unknown): Promise<unknown>;
}

export interface ToolCallOutcome {
  success: boolean;
  result?: unknown;
  error?: { code: string; message: string; recoverable: boolean };
}

export class ToolRegistry {
  private tools: Map<string, JarvisTool> = new Map();
  private readonly permissions: PermissionManager;
  private readonly events: EventBus | undefined;

  constructor(permissions: PermissionManager, events?: EventBus) {
    this.permissions = permissions;
    this.events = events;
  }

  register(tool: JarvisTool): void {
    if (this.tools.has(tool.name)) {
      throw new JarvisError({
        code: "DUPLICATE_TOOL",
        message: `Tool '${tool.name}' is already registered.`,
        source: "ToolRegistry",
        recoverable: false,
      });
    }
    this.tools.set(tool.name, tool);
  }

  get(name: string): JarvisTool | undefined {
    return this.tools.get(name);
  }

  list(): JarvisTool[] {
    return [...this.tools.values()];
  }

  /** Claude's tools[] array shape for the Messages API. */
  describeForClaude(): { name: string; description: string; input_schema: Record<string, unknown> }[] {
    return this.list().map((t) => ({ name: t.name, description: t.description, input_schema: t.inputSchema }));
  }

  async invoke(name: string, rawInput: unknown, options?: { confirmed?: boolean; callId?: string }): Promise<ToolCallOutcome> {
    const callId = options?.callId ?? `${Date.now()}`;
    const tool = this.tools.get(name);
    if (!tool) {
      return { success: false, error: { code: "UNKNOWN_TOOL", message: `No tool named '${name}' is registered.`, recoverable: false } };
    }

    const permission = this.permissions.check({ toolName: name, level: tool.level, confirmed: options?.confirmed });
    if (!permission.allowed) {
      return { success: false, error: { code: "PERMISSION_DENIED", message: permission.reason, recoverable: true } };
    }

    const parsed = tool.zodSchema.safeParse(rawInput);
    if (!parsed.success) {
      return {
        success: false,
        error: { code: "INVALID_INPUT", message: `Input for '${name}' failed validation: ${parsed.error.message}`, recoverable: true },
      };
    }

    this.events?.emit("agent.tool_call", { tool: name, input: parsed.data, callId });
    try {
      const result = await tool.execute(parsed.data);
      this.events?.emit("agent.tool_result", { tool: name, callId, success: true, result });
      return { success: true, result };
    } catch (err) {
      const jerr = err instanceof JarvisError ? err : new JarvisError({
        code: "TOOL_EXECUTION_FAILED",
        message: err instanceof Error ? err.message : String(err),
        source: name,
        recoverable: true,
      });
      this.events?.emit("agent.tool_result", { tool: name, callId, success: false, error: jerr.toJSON() });
      return { success: false, error: { code: jerr.code, message: jerr.message, recoverable: jerr.recoverable } };
    }
  }
}
