/**
 * Full-stack integration test: RustBridge -> createWindowsTools -> ToolRegistry,
 * against the REAL compiled native-core.exe and a real Notepad window on this
 * desktop. No mocks. This is what actually proves the TypeScript orchestrator
 * and the Rust core work together, not just independently.
 */
import { afterEach, describe, expect, it } from "vitest";

import { RustBridge } from "../src/rustBridge.ts";
import { PermissionManager } from "../src/PermissionManager.ts";
import { ToolRegistry } from "../src/ToolRegistry.ts";
import { createWindowsTools } from "../src/tools/windowsTools.ts";

function setup() {
  const rust = new RustBridge();
  const registry = new ToolRegistry(new PermissionManager());
  for (const tool of createWindowsTools(rust)) registry.register(tool);
  return { rust, registry };
}

let activeRust: RustBridge | null = null;

afterEach(() => {
  activeRust?.stop();
  activeRust = null;
});

describe("full stack (RustBridge + ToolRegistry + windowsTools) against real native-core", () => {
  it("list_windows returns real windows from this desktop", async () => {
    const { rust, registry } = setup();
    activeRust = rust;

    const outcome = await registry.invoke("list_windows", { query: "" });
    expect(outcome.success).toBe(true);
    const windows = (outcome.result as any).windows as unknown[];
    expect(Array.isArray(windows)).toBe(true);
    expect(windows.length).toBeGreaterThan(0);
  });

  it("open_application -> type_into_element -> read_element -> close_application: full Notepad lifecycle through the tool layer", async () => {
    const { rust, registry } = setup();
    activeRust = rust;

    const launch = await registry.invoke("open_application", { app_name: "Notepad" });
    expect(launch.success).toBe(true);
    expect((launch.result as any).window).toBeTruthy();

    const typed = await registry.invoke("type_into_element", {
      query: "notepad",
      element_query: "",
      control_type: "Document",
      text: "agent-ts full stack test marker",
      clear_first: true,
    });
    expect(typed.success).toBe(true);

    const read = await registry.invoke("read_element", { query: "notepad", element_query: "", control_type: "Document" });
    expect(read.success).toBe(true);
    expect((read.result as any).text).toContain("agent-ts full stack test marker");

    const closed = await registry.invoke("close_application", { query: "notepad", force: true });
    expect(closed.success).toBe(true);

    await new Promise((r) => setTimeout(r, 500));
  });

  it("kill_process refuses a protected system process through the full tool layer", async () => {
    const { rust, registry } = setup();
    activeRust = rust;

    const list = await registry.invoke("list_processes", { name_filter: "explorer.exe" });
    expect(list.success).toBe(true);
    const procs = (list.result as any).processes as { pid: number }[];
    if (procs.length === 0) return; // nothing to assert against on this machine

    const outcome = await registry.invoke("kill_process", { pid: procs[0]!.pid });
    expect(outcome.success).toBe(false);
    expect(outcome.error?.code).toBe("REFUSED");
  });

  it("a bad tool input is rejected by zod before it ever reaches native-core", async () => {
    const { rust, registry } = setup();
    activeRust = rust;

    const outcome = await registry.invoke("open_application", { app_name: "" }); // fails z.string().min(1)
    expect(outcome.success).toBe(false);
    expect(outcome.error?.code).toBe("INVALID_INPUT");
  });

  it("focus_window on a nonexistent app fails cleanly through the whole stack", async () => {
    const { rust, registry } = setup();
    activeRust = rust;

    const outcome = await registry.invoke("focus_window", { query: "ThisAppDefinitelyDoesNotExist_9f8e7d6c" });
    expect(outcome.success).toBe(false);
    expect(outcome.error?.code).toBe("NOT_FOUND");
  });
});
