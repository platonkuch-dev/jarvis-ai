#!/usr/bin/env node
/**
 * Dev/demo entry point for the TypeScript orchestrator.
 *
 *   node src/cli.ts rpc window.list '{}'
 *       -> calls native-core directly, no Claude/API key needed. Useful for
 *          proving the Rust<->TS bridge and individual tools work.
 *
 *   node src/cli.ts agent "list the open windows"
 *       -> runs the full Claude tool-calling agent loop against the
 *          Windows-control tools. Needs an Anthropic API key (see config.ts:
 *          ANTHROPIC_API_KEY env var, or claude_api_key in the same
 *          %APPDATA%\Jarvis\api_keys.json the Python app already uses).
 */
import Anthropic from "@anthropic-ai/sdk";

import { RustBridge } from "./rustBridge.ts";
import { EventBus } from "./EventBus.ts";
import { PermissionManager } from "./PermissionManager.ts";
import { ToolRegistry } from "./ToolRegistry.ts";
import { createWindowsTools } from "./tools/windowsTools.ts";
import { Agent } from "./Agent.ts";
import { getClaudeApiKey, getAnthropicWorkspaceId } from "./config.ts";
import { JarvisError } from "./protocol.ts";

async function runRpc(method: string, paramsJson: string): Promise<void> {
  const rust = new RustBridge();
  try {
    const params = paramsJson ? JSON.parse(paramsJson) : {};
    const result = await rust.call(method, params);
    console.log(JSON.stringify(result, null, 2));
  } finally {
    rust.stop();
  }
}

async function runAgent(goal: string): Promise<void> {
  const apiKey = getClaudeApiKey();
  if (!apiKey) {
    console.error(
      "No Claude API key found. Set ANTHROPIC_API_KEY, or add \"claude_api_key\" to " +
        "%APPDATA%\\Jarvis\\api_keys.json (the same file the Python JARVIS app uses).",
    );
    process.exitCode = 1;
    return;
  }

  const rust = new RustBridge();
  const events = new EventBus();
  events.on("agent.thinking", (p) => console.error(`[agent] thinking (${p.note ?? ""})`));
  events.on("agent.tool_call", (p) => console.error(`[agent] -> ${p.tool}(${JSON.stringify(p.input)})`));
  events.on("agent.tool_result", (p) =>
    console.error(`[agent] <- ${p.tool} ${p.success ? "OK" : "FAILED: " + JSON.stringify(p.error)}`),
  );

  const permissions = new PermissionManager();
  const registry = new ToolRegistry(permissions, events);
  for (const tool of createWindowsTools(rust)) registry.register(tool);

  const workspaceId = getAnthropicWorkspaceId();
  const client = new Anthropic({
    apiKey,
    defaultHeaders: workspaceId ? { "anthropic-workspace-id": workspaceId } : undefined,
  });
  const agent = new Agent(client, registry, {}, events);

  try {
    const answer = await agent.run(goal);
    console.log(answer);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    if (!workspaceId && message.includes("anthropic-workspace-id is required")) {
      console.error(
        "This API key is identity-linked and needs a workspace id. Add \"anthropic_workspace_id\" to " +
          "%APPDATA%\\Jarvis\\api_keys.json (or set ANTHROPIC_WORKSPACE_ID) — find the id in " +
          "console.anthropic.com under the target workspace's settings, in the page URL.",
      );
      process.exitCode = 1;
      return;
    }
    throw err;
  } finally {
    rust.stop();
  }
}

async function main(): Promise<void> {
  const [mode, ...rest] = process.argv.slice(2);

  try {
    if (mode === "rpc") {
      const [method, paramsJson] = rest;
      if (!method) throw new Error("Usage: node src/cli.ts rpc <method> [paramsJson]");
      await runRpc(method, paramsJson ?? "{}");
    } else if (mode === "agent") {
      const goal = rest.join(" ");
      if (!goal) throw new Error('Usage: node src/cli.ts agent "<goal>"');
      await runAgent(goal);
    } else {
      console.error('Usage:\n  node src/cli.ts rpc <method> [paramsJson]\n  node src/cli.ts agent "<goal>"');
      process.exitCode = 1;
    }
  } catch (err) {
    if (err instanceof JarvisError) {
      console.error(`[${err.code}] ${err.message}`);
    } else {
      console.error(err);
    }
    process.exitCode = 1;
  }
}

main();
