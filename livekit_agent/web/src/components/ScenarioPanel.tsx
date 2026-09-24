import { AnimatePresence, motion } from "framer-motion";
import type { RunningScenario, Scenario } from "../lib/types";

interface ScenarioPanelProps {
  scenarios: Scenario[];
  runningScenario: RunningScenario | null;
  onRun: (name: string) => void;
  disabled: boolean;
}

export function ScenarioPanel({ scenarios, runningScenario, onRun, disabled }: ScenarioPanelProps) {
  if (scenarios.length === 0) {
    return (
      <div className="px-3 py-6 text-center text-sm text-ink-dim">
        Сценариев пока нет. Опишите голосом рабочий процесс — Джарвис запомнит его сам.
      </div>
    );
  }

  return (
    <ul className="flex flex-col gap-2 overflow-y-auto px-1 py-1">
      {scenarios.map((scenario) => {
        const isRunning = runningScenario?.name === scenario.name && !runningScenario.finished;
        return (
          <li
            key={scenario.name}
            className="rounded-lg border border-line bg-panel px-3 py-2.5 transition-colors"
          >
            <button
              type="button"
              disabled={disabled || isRunning}
              onClick={() => onRun(scenario.name)}
              className="flex w-full items-center justify-between gap-2 text-left disabled:cursor-not-allowed"
            >
              <span>
                <span className="block text-[13.5px] font-medium text-ink">{scenario.name}</span>
                <span className="block text-xs text-ink-dim">«{scenario.trigger_phrase}»</span>
              </span>
              <span className="shrink-0 text-xs text-ink-dim">{scenario.steps.length} шаг(ов)</span>
            </button>

            <AnimatePresence>
              {isRunning && (
                <motion.ul
                  initial={{ height: 0, opacity: 0 }}
                  animate={{ height: "auto", opacity: 1 }}
                  exit={{ height: 0, opacity: 0 }}
                  className="mt-2.5 overflow-hidden border-t border-line pt-2.5"
                >
                  {runningScenario.steps.map((step, i) => (
                    <li key={i} className="mb-1.5 flex items-center gap-2 text-xs last:mb-0">
                      <StepMarker status={step.status} />
                      <span className={step.status === "pending" ? "text-ink-dim" : "text-ink"}>
                        {step.tool || `шаг ${i + 1}`}
                      </span>
                    </li>
                  ))}
                </motion.ul>
              )}
            </AnimatePresence>
          </li>
        );
      })}
    </ul>
  );
}

function StepMarker({ status }: { status: string }) {
  if (status === "done") {
    return (
      <span className="flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-full bg-signal text-[9px] font-bold text-bg">
        ✓
      </span>
    );
  }
  if (status === "running") {
    return <span className="h-3.5 w-3.5 shrink-0 animate-pulse rounded-full border-2 border-signal" />;
  }
  if (status === "error" || status === "skipped") {
    return (
      <span className="flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-full border border-ink-dim text-[9px] text-ink-dim">
        !
      </span>
    );
  }
  return <span className="h-3.5 w-3.5 shrink-0 rounded-full border border-line" />;
}
