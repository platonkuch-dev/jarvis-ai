/**
 * Reads the SAME credential file the Python side already uses
 * (core/path_utils.py's get_config_path(): %APPDATA%\Jarvis\api_keys.json)
 * instead of inventing a second place to store secrets. No key is ever
 * hardcoded here or logged — see the "no secrets in logs" project rule.
 */
import { readFileSync, existsSync } from "node:fs";
import path from "node:path";

export interface JarvisConfig {
  claude_api_key?: string;
  gemini_api_key?: string;
  anthropic_workspace_id?: string;
  [key: string]: unknown;
}

export function configPath(): string {
  const appData = process.env["APPDATA"] ?? path.join(process.env["USERPROFILE"] ?? ".", "AppData", "Roaming");
  return path.join(appData, "Jarvis", "api_keys.json");
}

export function loadConfig(): JarvisConfig {
  const p = configPath();
  if (!existsSync(p)) return {};
  try {
    return JSON.parse(readFileSync(p, "utf-8")) as JarvisConfig;
  } catch {
    return {};
  }
}

export function getClaudeApiKey(): string | undefined {
  return process.env["ANTHROPIC_API_KEY"] ?? loadConfig().claude_api_key ?? undefined;
}

/**
 * Required alongside an "identity-linked" Anthropic API key (the kind an
 * org can be configured to issue exclusively, instead of standalone
 * workspace keys) — the API rejects requests from such a key with 400
 * "anthropic-workspace-id is required" until this is sent as a header.
 * Find it in console.anthropic.com under the target workspace's settings
 * (it's the path segment in that page's URL).
 */
export function getAnthropicWorkspaceId(): string | undefined {
  return process.env["ANTHROPIC_WORKSPACE_ID"] ?? loadConfig().anthropic_workspace_id ?? undefined;
}
