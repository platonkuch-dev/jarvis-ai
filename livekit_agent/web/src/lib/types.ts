export type AgentStatus = "connecting" | "idle" | "listening" | "thinking" | "speaking" | "disconnected";

export type TranscriptRole = "user" | "assistant";

export interface TranscriptLine {
  id: string;
  role: TranscriptRole;
  text: string;
  final: boolean;
  ts: number;
}

export interface ScenarioStep {
  tool: string;
  args: Record<string, unknown>;
}

export interface Scenario {
  name: string;
  trigger_phrase: string;
  steps: ScenarioStep[];
}

export interface ActionLogEntry {
  ts: string;
  tool: string;
  args: Record<string, unknown>;
  ok: boolean;
  result?: unknown;
  error?: string;
  duration_ms?: number;
}

export type StepStatus = "pending" | "running" | "done" | "error" | "skipped";

export interface RunningScenario {
  name: string;
  totalSteps: number;
  steps: { tool: string; status: StepStatus; message: string }[];
  finished: boolean;
}

/** Raw shape published by tools/scenarios.py on the "lk.scenario_progress" data topic. */
export interface ScenarioProgressEvent {
  scenario: string;
  step_index: number;
  total_steps: number;
  tool: string;
  status: "started" | "running" | "done" | "error" | "skipped" | "finished";
  message: string;
}
