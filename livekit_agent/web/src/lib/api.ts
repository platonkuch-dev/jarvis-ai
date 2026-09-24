import type { ActionLogEntry, Scenario } from "./types";

// web_api.py binds to localhost only; see ../../../web_api.py
const API_BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8787";

export interface TokenResponse {
  token: string;
  url: string;
  room: string;
  identity: string;
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) {
    throw new Error(`${path} -> ${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

export function fetchToken(identity: string): Promise<TokenResponse> {
  return getJson<TokenResponse>(`/api/token?identity=${encodeURIComponent(identity)}`);
}

export function fetchScenarios(): Promise<Record<string, Omit<Scenario, "name">>> {
  return getJson(`/api/scenarios`);
}

export function fetchActions(limit = 50): Promise<ActionLogEntry[]> {
  return getJson(`/api/actions?limit=${limit}`);
}
