/**
 * The Windows-control tool surface Claude actually sees. Every tool here is
 * a thin, schema-validated wrapper around one native-core RPC method
 * (see native-core/src/dispatch.rs for the authoritative method list) —
 * no tool does anything Rust/Win32 didn't already implement and test.
 */
import { z } from "zod";

import type { RustBridge } from "../rustBridge.ts";
import type { JarvisTool } from "../ToolRegistry.ts";
import type { PermissionLevel } from "../PermissionManager.ts";

function defineTool(params: {
  name: string;
  description: string;
  level: PermissionLevel;
  inputSchema: Record<string, unknown>;
  zodSchema: z.ZodType;
  execute: (input: any) => Promise<unknown>;
}): JarvisTool {
  return {
    name: params.name,
    description: params.description,
    level: params.level,
    inputSchema: params.inputSchema,
    zodSchema: params.zodSchema,
    execute: params.execute,
  };
}

const controlTypeEnum = [
  "Button", "Edit", "Text", "CheckBox", "RadioButton", "ComboBox", "List", "ListItem",
  "Menu", "MenuItem", "Tab", "TabItem", "Tree", "TreeItem", "Window", "Pane", "Hyperlink",
  "Document", "Image", "Group", "ToolBar",
] as const;

/** Builds the full Windows-control tool set, bound to one RustBridge instance. */
export function createWindowsTools(rust: RustBridge): JarvisTool[] {
  return [
    defineTool({
      name: "open_application",
      description:
        "Launches an application by name, or focuses it if it's already running. Handles apps not on PATH via the Windows App Paths registry and file associations (e.g. 'ms-settings:' for Windows Settings). Also opens well-known folders: downloads, desktop, documents, home.",
      level: "NORMAL",
      inputSchema: {
        type: "object",
        properties: { app_name: { type: "string", description: "e.g. 'Discord', 'Chrome', 'Telegram', 'downloads'" } },
        required: ["app_name"],
      },
      zodSchema: z.object({ app_name: z.string().min(1) }),
      execute: async (input: { app_name: string }) => rust.call("process.launch", { app_name: input.app_name, timeout_ms: 8000 }),
    }),

    defineTool({
      name: "close_application",
      description: "Closes a specific application's window by name/title (asks it to close gracefully; set force=true to kill the process if it doesn't close on its own).",
      level: "NORMAL",
      inputSchema: {
        type: "object",
        properties: {
          query: { type: "string", description: "App name or window title fragment" },
          force: { type: "boolean", description: "Kill the process if a graceful close doesn't work (default false)" },
        },
        required: ["query"],
      },
      zodSchema: z.object({ query: z.string().min(1), force: z.boolean().optional() }),
      execute: async (input: { query: string; force?: boolean }) => rust.call("window.close", input),
    }),

    defineTool({
      name: "kill_process",
      description: "Force-terminates a process by PID. More forceful than close_application — use only when a normal close isn't appropriate. Protected system processes (explorer.exe, csrss.exe, etc.) are always refused.",
      level: "SENSITIVE",
      inputSchema: {
        type: "object",
        properties: { pid: { type: "integer", description: "Process ID" } },
        required: ["pid"],
      },
      zodSchema: z.object({ pid: z.number().int().positive() }),
      execute: async (input: { pid: number }) => rust.call("process.kill", { pid: input.pid, force: true }),
    }),

    defineTool({
      name: "focus_window",
      description: "Brings a specific application's window to the foreground ('switch to Chrome').",
      level: "SAFE",
      inputSchema: { type: "object", properties: { query: { type: "string" } }, required: ["query"] },
      zodSchema: z.object({ query: z.string().min(1) }),
      execute: async (input: { query: string }) => rust.call("window.focus", input),
    }),

    defineTool({
      name: "minimize_window",
      description: "Minimizes a specific application's window.",
      level: "SAFE",
      inputSchema: { type: "object", properties: { query: { type: "string" } }, required: ["query"] },
      zodSchema: z.object({ query: z.string().min(1) }),
      execute: async (input: { query: string }) => rust.call("window.minimize", input),
    }),

    defineTool({
      name: "maximize_window",
      description: "Maximizes a specific application's window.",
      level: "SAFE",
      inputSchema: { type: "object", properties: { query: { type: "string" } }, required: ["query"] },
      zodSchema: z.object({ query: z.string().min(1) }),
      execute: async (input: { query: string }) => rust.call("window.maximize", input),
    }),

    defineTool({
      name: "restore_window",
      description: "Restores a minimized/maximized window to its normal state.",
      level: "SAFE",
      inputSchema: { type: "object", properties: { query: { type: "string" } }, required: ["query"] },
      zodSchema: z.object({ query: z.string().min(1) }),
      execute: async (input: { query: string }) => rust.call("window.restore", input),
    }),

    defineTool({
      name: "move_window",
      description: "Moves a specific application's window to given screen coordinates.",
      level: "NORMAL",
      inputSchema: {
        type: "object",
        properties: { query: { type: "string" }, x: { type: "integer" }, y: { type: "integer" } },
        required: ["query"],
      },
      zodSchema: z.object({ query: z.string().min(1), x: z.number().int().optional(), y: z.number().int().optional() }),
      execute: async (input: { query: string; x?: number; y?: number }) => rust.call("window.move", input),
    }),

    defineTool({
      name: "resize_window",
      description: "Resizes a specific application's window.",
      level: "NORMAL",
      inputSchema: {
        type: "object",
        properties: { query: { type: "string" }, width: { type: "integer" }, height: { type: "integer" } },
        required: ["query"],
      },
      zodSchema: z.object({ query: z.string().min(1), width: z.number().int().positive().optional(), height: z.number().int().positive().optional() }),
      execute: async (input: { query: string; width?: number; height?: number }) => rust.call("window.resize", input),
    }),

    defineTool({
      name: "list_windows",
      description: "Lists open windows, optionally filtered by app name/title fragment. Use an empty query to list everything.",
      level: "SAFE",
      inputSchema: { type: "object", properties: { query: { type: "string" } } },
      zodSchema: z.object({ query: z.string().optional() }),
      execute: async (input: { query?: string }) => rust.call("window.list", { query: input.query ?? "" }),
    }),

    defineTool({
      name: "list_processes",
      description: "Lists running processes, optionally filtered by name.",
      level: "SAFE",
      inputSchema: { type: "object", properties: { name_filter: { type: "string" } } },
      zodSchema: z.object({ name_filter: z.string().optional() }),
      execute: async (input: { name_filter?: string }) => rust.call("process.list", { name_filter: input.name_filter ?? "" }),
    }),

    defineTool({
      name: "get_active_window",
      description: "Returns the currently focused/foreground window.",
      level: "SAFE",
      inputSchema: { type: "object", properties: {} },
      zodSchema: z.object({}),
      execute: async () => rust.call("window.get_active", {}),
    }),

    defineTool({
      name: "wait_for_window",
      description: "Waits (polling, not a blind sleep) for a window matching the query to appear — use after launching something slow instead of guessing a delay.",
      level: "SAFE",
      inputSchema: {
        type: "object",
        properties: { query: { type: "string" }, timeout_ms: { type: "integer", description: "Default 10000" } },
        required: ["query"],
      },
      zodSchema: z.object({ query: z.string().min(1), timeout_ms: z.number().int().positive().optional() }),
      execute: async (input: { query: string; timeout_ms?: number }) => rust.call("window.wait_for", input),
    }),

    defineTool({
      name: "find_ui_element",
      description: "Finds a UI control (button, field, menu item, ...) inside a window by name and/or control type, via Windows UI Automation.",
      level: "SAFE",
      inputSchema: {
        type: "object",
        properties: {
          query: { type: "string", description: "Which window to search in" },
          element_query: { type: "string", description: "Name/text/automation-id substring to match" },
          control_type: { type: "string", enum: controlTypeEnum },
          max_depth: { type: "integer", description: "Default 6" },
          index: { type: "integer", description: "Which match to use if several, default 0" },
        },
        required: ["query"],
      },
      zodSchema: z.object({
        query: z.string().min(1), element_query: z.string().optional(), control_type: z.enum(controlTypeEnum).optional(),
        max_depth: z.number().int().positive().optional(), index: z.number().int().nonnegative().optional(),
      }),
      execute: async (input) => rust.call("ui.find_element", input),
    }),

    defineTool({
      name: "click_element",
      description: "Clicks a UI control inside a window, found by name/control_type via UI Automation.",
      level: "NORMAL",
      inputSchema: {
        type: "object",
        properties: {
          query: { type: "string" }, element_query: { type: "string" }, control_type: { type: "string", enum: controlTypeEnum },
          double: { type: "boolean" }, right: { type: "boolean" }, max_depth: { type: "integer" },
        },
        required: ["query"],
      },
      zodSchema: z.object({
        query: z.string().min(1), element_query: z.string().optional(), control_type: z.enum(controlTypeEnum).optional(),
        double: z.boolean().optional(), right: z.boolean().optional(), max_depth: z.number().int().positive().optional(),
      }),
      execute: async (input) => rust.call("ui.click_element", input),
    }),

    defineTool({
      name: "type_into_element",
      description: "Types text into a specific UI field inside a window, found by name/control_type via UI Automation.",
      level: "NORMAL",
      inputSchema: {
        type: "object",
        properties: {
          query: { type: "string" }, element_query: { type: "string" }, control_type: { type: "string", enum: controlTypeEnum },
          text: { type: "string" }, clear_first: { type: "boolean", description: "Default true" },
        },
        required: ["query", "text"],
      },
      zodSchema: z.object({
        query: z.string().min(1), element_query: z.string().optional(), control_type: z.enum(controlTypeEnum).optional(),
        text: z.string(), clear_first: z.boolean().optional(),
      }),
      execute: async (input) => rust.call("ui.type_into_element", input),
    }),

    defineTool({
      name: "read_element",
      description: "Reads the text/value of a UI control inside a window.",
      level: "SAFE",
      inputSchema: {
        type: "object",
        properties: { query: { type: "string" }, element_query: { type: "string" }, control_type: { type: "string", enum: controlTypeEnum } },
        required: ["query"],
      },
      zodSchema: z.object({ query: z.string().min(1), element_query: z.string().optional(), control_type: z.enum(controlTypeEnum).optional() }),
      execute: async (input) => rust.call("ui.read_element", input),
    }),

    defineTool({
      name: "clear_element",
      description: "Clears a UI field's content inside a window.",
      level: "NORMAL",
      inputSchema: {
        type: "object",
        properties: { query: { type: "string" }, element_query: { type: "string" }, control_type: { type: "string", enum: controlTypeEnum } },
        required: ["query"],
      },
      zodSchema: z.object({ query: z.string().min(1), element_query: z.string().optional(), control_type: z.enum(controlTypeEnum).optional() }),
      execute: async (input) => rust.call("ui.clear_element", input),
    }),

    defineTool({
      name: "get_ui_tree",
      description: "Returns a bounded, depth-limited accessibility tree of a window's controls — use to understand an app's UI before clicking/typing into it.",
      level: "SAFE",
      inputSchema: {
        type: "object",
        properties: {
          query: { type: "string" }, max_depth: { type: "integer", description: "Default 6" },
          max_elements: { type: "integer", description: "Default 150" },
        },
        required: ["query"],
      },
      zodSchema: z.object({ query: z.string().min(1), max_depth: z.number().int().positive().optional(), max_elements: z.number().int().positive().optional() }),
      execute: async (input) => rust.call("ui.get_tree", input),
    }),

    defineTool({
      name: "wait_for_ui_element",
      description: "Waits (polling) for a UI control to appear inside a window, instead of guessing a fixed delay.",
      level: "SAFE",
      inputSchema: {
        type: "object",
        properties: {
          query: { type: "string" }, element_query: { type: "string" }, control_type: { type: "string", enum: controlTypeEnum },
          timeout_ms: { type: "integer", description: "Default 8000" },
        },
        required: ["query"],
      },
      zodSchema: z.object({
        query: z.string().min(1), element_query: z.string().optional(), control_type: z.enum(controlTypeEnum).optional(),
        timeout_ms: z.number().int().positive().optional(),
      }),
      execute: async (input) => rust.call("ui.wait_for_element", input),
    }),
  ];
}
