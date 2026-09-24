import { AnimatePresence, motion } from "framer-motion";
import type { ActionLogEntry } from "../lib/types";

interface ActionLogProps {
  entries: ActionLogEntry[];
  expanded: boolean;
  onToggle: () => void;
}

function resultText(entry: ActionLogEntry): string {
  if (!entry.ok) return entry.error ?? "ошибка";
  const r = entry.result as { message?: string } | undefined;
  return r?.message ?? "готово";
}

function formatTime(ts: string): string {
  // ts is "YYYY-MM-DDTHH:MM:SS" from tools/_logging.py
  const match = /T(\d{2}:\d{2}:\d{2})/.exec(ts);
  return match ? match[1] : ts;
}

export function ActionLog({ entries, expanded, onToggle }: ActionLogProps) {
  const recent = entries.slice(-60).reverse();

  return (
    <div>
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center justify-between px-1 py-1 text-xs text-ink-dim"
      >
        <span>Журнал действий</span>
        <span className="font-mono">{expanded ? "▾" : "▸"}</span>
      </button>

      <AnimatePresence>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="overflow-hidden"
          >
            <div className="max-h-64 overflow-y-auto rounded-lg border border-line bg-panel px-3 py-2 font-mono text-[11px] leading-relaxed">
              {recent.length === 0 && <div className="text-ink-dim">пока пусто</div>}
              {recent.map((entry, i) => (
                <div key={i} className="flex gap-2">
                  <span className="shrink-0 text-ink-dim">{formatTime(entry.ts)}</span>
                  <span className={entry.ok ? "shrink-0 text-signal" : "shrink-0 text-ink-dim"}>
                    {entry.tool}
                  </span>
                  <span className="truncate text-ink-dim">{resultText(entry)}</span>
                </div>
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
