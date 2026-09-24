import { useEffect, useRef } from "react";
import type { AgentStatus } from "../lib/types";

interface OrbProps {
  status: AgentStatus;
  micLevelRef: React.MutableRefObject<number>;
  agentLevelRef: React.MutableRefObject<number>;
}

const SIGNAL = "127, 216, 238"; // --color-signal as an rgb triplet, for alpha compositing

/**
 * All four states share one hue and one shape -- they differ only in motion
 * pattern and intensity (per the brief's "one accent color" instruction):
 *   idle      - slow breathing, near-static
 *   listening - a ring that deforms tightly around the core with mic amplitude
 *   thinking  - particles orbiting the core, independent of any audio level
 *   speaking  - ripples broadcasting outward, paced by the agent's own audio level
 */
export function Orb({ status, micLevelRef, agentLevelRef }: OrbProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const statusRef = useRef(status);
  statusRef.current = status;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let raf = 0;
    let t = 0;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);

    const resize = () => {
      const size = canvas.clientWidth;
      canvas.width = size * dpr;
      canvas.height = size * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(canvas);

    const render = () => {
      const size = canvas.clientWidth;
      const cx = size / 2;
      const cy = size / 2;
      const baseR = size * 0.16;
      const st = statusRef.current;
      const mic = micLevelRef.current;
      const agent = agentLevelRef.current;

      t += 1;
      ctx.clearRect(0, 0, size, size);

      let coreScale = 1;
      let glowAlpha = 0.16;

      if (st === "idle" || st === "connecting" || st === "disconnected") {
        coreScale = 1 + Math.sin(t * 0.02) * 0.035;
        glowAlpha = 0.14 + Math.sin(t * 0.02) * 0.04;

        ctx.beginPath();
        ctx.arc(cx, cy, baseR * 1.9, 0, Math.PI * 2);
        ctx.strokeStyle = `rgba(${SIGNAL}, 0.12)`;
        ctx.lineWidth = 1;
        ctx.stroke();
      } else if (st === "listening") {
        coreScale = 1 + mic * 0.5;
        glowAlpha = 0.22 + mic * 0.5;

        const points = 64;
        ctx.beginPath();
        for (let i = 0; i <= points; i++) {
          const angle = (i / points) * Math.PI * 2;
          const wobble = Math.sin(angle * 5 + t * 0.12) * 0.5 + Math.sin(angle * 9 - t * 0.08) * 0.5;
          const r = baseR * 1.9 + wobble * (4 + mic * 22);
          const x = cx + Math.cos(angle) * r;
          const y = cy + Math.sin(angle) * r;
          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        }
        ctx.closePath();
        ctx.strokeStyle = `rgba(${SIGNAL}, ${0.35 + mic * 0.5})`;
        ctx.lineWidth = 1.5;
        ctx.stroke();
      } else if (st === "thinking") {
        coreScale = 1 + Math.sin(t * 0.05) * 0.05;
        glowAlpha = 0.2;

        const particles = 5;
        for (let i = 0; i < particles; i++) {
          const speed = 0.02 + i * 0.004;
          const radius = baseR * (1.7 + (i % 3) * 0.25);
          const angle = t * speed + (i * Math.PI * 2) / particles;
          const x = cx + Math.cos(angle) * radius;
          const y = cy + Math.sin(angle) * radius * 0.94;
          const alpha = 0.35 + 0.35 * Math.sin(t * 0.03 + i);
          ctx.beginPath();
          ctx.arc(x, y, 2.4, 0, Math.PI * 2);
          ctx.fillStyle = `rgba(${SIGNAL}, ${Math.max(0.15, alpha)})`;
          ctx.fill();
        }
      } else if (st === "speaking") {
        coreScale = 1 + agent * 0.4;
        glowAlpha = 0.24 + agent * 0.5;

        const ripples = 3;
        for (let i = 0; i < ripples; i++) {
          const phase = ((t * (0.9 + agent * 2.2)) / 60 + i / ripples) % 1;
          const r = baseR * (1.3 + phase * 2.4);
          const alpha = (1 - phase) * (0.28 + agent * 0.4);
          ctx.beginPath();
          ctx.arc(cx, cy, r, 0, Math.PI * 2);
          ctx.strokeStyle = `rgba(${SIGNAL}, ${Math.max(0, alpha)})`;
          ctx.lineWidth = 1.2;
          ctx.stroke();
        }
      }

      // Soft outer glow behind the core.
      const glow = ctx.createRadialGradient(cx, cy, 0, cx, cy, baseR * 3.2);
      glow.addColorStop(0, `rgba(${SIGNAL}, ${glowAlpha})`);
      glow.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = glow;
      ctx.fillRect(0, 0, size, size);

      // The core itself.
      const r = baseR * coreScale;
      const core = ctx.createRadialGradient(cx, cy, 0, cx, cy, r);
      core.addColorStop(0, `rgba(${SIGNAL}, 0.95)`);
      core.addColorStop(0.7, `rgba(${SIGNAL}, 0.55)`);
      core.addColorStop(1, `rgba(${SIGNAL}, 0.05)`);
      ctx.beginPath();
      ctx.arc(cx, cy, r, 0, Math.PI * 2);
      ctx.fillStyle = core;
      ctx.fill();

      raf = requestAnimationFrame(render);
    };
    raf = requestAnimationFrame(render);

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
    };
    // Refs are stable identities; the render loop reads their .current live.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <canvas
      ref={canvasRef}
      className="aspect-square w-full max-w-[240px]"
      role="img"
      aria-label={`Джарвис: ${status}`}
    />
  );
}
