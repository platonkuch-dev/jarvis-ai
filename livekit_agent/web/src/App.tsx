import { useEffect, useMemo, useState } from "react";
import { ActionLog } from "./components/ActionLog";
import { Orb } from "./components/Orb";
import { ScenarioPanel } from "./components/ScenarioPanel";
import { TranscriptFeed } from "./components/TranscriptFeed";
import { fetchActions, fetchScenarios } from "./lib/api";
import { useLiveKitRoom } from "./lib/useLiveKitRoom";
import type { ActionLogEntry, AgentStatus, Scenario } from "./lib/types";

const STATUS_LABEL: Record<AgentStatus, string> = {
  connecting: "подключение…",
  idle: "Джарвис",
  listening: "слушаю",
  thinking: "думаю",
  speaking: "говорю",
  disconnected: "не в сети",
};

const POLL_MS = 4000;

export default function App() {
  const identity = useMemo(() => `web-${Math.random().toString(36).slice(2, 8)}`, []);
  const { status, transcript, error, micLevelRef, agentLevelRef, sendText, runningScenario } =
    useLiveKitRoom(identity);

  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [actions, setActions] = useState<ActionLogEntry[]>([]);
  const [logExpanded, setLogExpanded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const [scenarioMap, actionList] = await Promise.all([fetchScenarios(), fetchActions(60)]);
        if (cancelled) return;
        setScenarios(Object.entries(scenarioMap).map(([name, s]) => ({ name, ...s })));
        setActions(actionList);
      } catch {
        // The local API may not be running yet; keep the room connection independent of it.
      }
    };
    void load();
    const id = setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [runningScenario?.finished]);

  const handleRunScenario = (name: string) => {
    void sendText(`Запусти сценарий «${name}».`);
  };

  const canInteract = status !== "connecting" && status !== "disconnected";

  return (
    <div className="flex min-h-screen w-screen flex-col overflow-y-auto bg-bg text-ink lg:h-screen lg:flex-row lg:overflow-hidden">
      <aside className="order-3 border-line px-4 py-3 lg:order-1 lg:h-full lg:w-64 lg:shrink-0 lg:border-r lg:py-5">
        <h2 className="mb-2 px-1 text-xs text-ink-dim">Сценарии</h2>
        <div className="max-h-[34vh] overflow-y-auto lg:max-h-[calc(100vh-3.5rem)]">
          <ScenarioPanel
            scenarios={scenarios}
            runningScenario={runningScenario}
            onRun={handleRunScenario}
            disabled={!canInteract}
          />
        </div>
      </aside>

      <main className="order-1 flex min-h-0 flex-1 flex-col items-center justify-center gap-5 px-6 py-8 lg:order-2">
        <Orb status={status} micLevelRef={micLevelRef} agentLevelRef={agentLevelRef} />

        <div className="flex items-center gap-2 text-sm">
          <span
            className="h-1.5 w-1.5 rounded-full bg-signal transition-opacity"
            style={{ opacity: status === "idle" || status === "connecting" ? 0.35 : 0.9 }}
          />
          <span className="text-ink-dim">{STATUS_LABEL[status]}</span>
        </div>

        {error && <p className="max-w-sm text-center text-xs text-ink-dim">{error}</p>}

        <div className="min-h-[30vh] w-full max-w-lg flex-1 lg:min-h-0">
          <TranscriptFeed lines={transcript} />
        </div>
      </main>

      <div className="order-2 border-line px-4 py-3 lg:order-3 lg:flex lg:h-full lg:w-72 lg:shrink-0 lg:flex-col lg:justify-end lg:border-l lg:py-5">
        <ActionLog entries={actions} expanded={logExpanded} onToggle={() => setLogExpanded((v) => !v)} />
      </div>
    </div>
  );
}
