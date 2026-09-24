import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useRef } from "react";
import type { TranscriptLine } from "../lib/types";

interface TranscriptFeedProps {
  lines: TranscriptLine[];
}

/** Captions-style log of the conversation -- no bubbles, no chrome, just speech. */
export function TranscriptFeed({ lines }: TranscriptFeedProps) {
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [lines]);

  const visible = lines.slice(-40);

  return (
    <div className="relative h-full overflow-hidden">
      <div className="pointer-events-none absolute inset-x-0 top-0 h-8 bg-gradient-to-b from-bg to-transparent" />
      <div className="h-full overflow-y-auto px-1 py-4">
        <AnimatePresence initial={false}>
          {visible.map((line) => (
            <motion.div
              key={line.id}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: line.final ? 1 : 0.55 }}
              transition={{ duration: 0.25 }}
              className="mb-2.5 flex items-baseline gap-3 text-[15px] leading-snug"
            >
              <span className="w-16 shrink-0 text-right text-xs text-ink-dim">
                {line.role === "user" ? "Вы" : "Джарвис"}
              </span>
              <span className={line.role === "assistant" ? "text-ink" : "text-ink"}>{line.text}</span>
            </motion.div>
          ))}
        </AnimatePresence>
        <div ref={endRef} />
      </div>
    </div>
  );
}
