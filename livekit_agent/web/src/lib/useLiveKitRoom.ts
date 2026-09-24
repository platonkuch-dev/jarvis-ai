import { useCallback, useEffect, useRef, useState } from "react";
import {
  Room,
  RoomEvent,
  Track,
  type RemoteParticipant,
  type RemoteTrack,
  type TextStreamReader,
  type TranscriptionSegment,
} from "livekit-client";

import { fetchToken } from "./api";
import { LevelAnalyser } from "./audioLevel";
import type { AgentStatus, RunningScenario, ScenarioProgressEvent, TranscriptLine } from "./types";

const SPEAK_THRESHOLD = 0.05;
const LISTEN_THRESHOLD = 0.045;
// Safety net: never show "thinking" forever if a reply never lands (e.g. an
// LLM/tool error swallowed server-side) -- fall back to idle instead of
// leaving the orb stuck mid-thought.
const THINKING_TIMEOUT_MS = 15000;
const SCENARIO_PROGRESS_TOPIC = "lk.scenario_progress";

export interface LiveKitRoomState {
  status: AgentStatus;
  transcript: TranscriptLine[];
  error: string | null;
  micLevelRef: React.MutableRefObject<number>;
  agentLevelRef: React.MutableRefObject<number>;
  sendText: (text: string) => Promise<void>;
  runningScenario: RunningScenario | null;
}

export function useLiveKitRoom(identity: string): LiveKitRoomState {
  const roomRef = useRef<Room | null>(null);
  const micAnalyserRef = useRef<LevelAnalyser | null>(null);
  const agentAnalyserRef = useRef<LevelAnalyser | null>(null);
  const micLevelRef = useRef(0);
  const agentLevelRef = useRef(0);
  const mediaElRef = useRef<HTMLMediaElement | null>(null);
  const thinkingSinceRef = useRef<number | null>(null);
  const statusRef = useRef<AgentStatus>("connecting");

  const [status, setStatus] = useState<AgentStatus>("connecting");
  const [transcript, setTranscript] = useState<TranscriptLine[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [runningScenario, setRunningScenario] = useState<RunningScenario | null>(null);

  const applyScenarioEvent = useCallback((ev: ScenarioProgressEvent) => {
    setRunningScenario((prev) => {
      if (ev.status === "started") {
        return {
          name: ev.scenario,
          totalSteps: ev.total_steps,
          steps: Array.from({ length: ev.total_steps }, () => ({
            tool: "",
            status: "pending" as const,
            message: "",
          })),
          finished: false,
        };
      }
      if (ev.status === "finished") {
        return prev ? { ...prev, finished: true } : prev;
      }
      if (!prev || prev.name !== ev.scenario) return prev;
      const steps = prev.steps.slice();
      if (ev.step_index >= 0 && ev.step_index < steps.length) {
        steps[ev.step_index] = { tool: ev.tool, status: ev.status, message: ev.message };
      }
      return { ...prev, steps };
    });
  }, []);

  const setStatusSafe = useCallback((next: AgentStatus) => {
    if (statusRef.current !== next) {
      statusRef.current = next;
      setStatus(next);
    }
  }, []);

  const upsertTranscriptLine = useCallback((line: TranscriptLine) => {
    setTranscript((prev) => {
      const idx = prev.findIndex((l) => l.id === line.id);
      if (idx === -1) return [...prev, line].slice(-200);
      const next = prev.slice();
      next[idx] = line;
      return next;
    });
  }, []);

  useEffect(() => {
    let cancelled = false;
    let rafId = 0;

    async function connect() {
      try {
        const { token, url } = await fetchToken(identity);
        if (cancelled) return;

        const room = new Room({ adaptiveStream: true, dynacast: true });
        roomRef.current = room;

        room.on(RoomEvent.TranscriptionReceived, (segments: TranscriptionSegment[], participant) => {
          const isLocal = participant?.isLocal ?? false;
          for (const seg of segments) {
            upsertTranscriptLine({
              id: seg.id,
              role: isLocal ? "user" : "assistant",
              text: seg.text,
              final: seg.final,
              ts: seg.lastReceivedTime || Date.now(),
            });
          }
          if (!isLocal) {
            // The agent has started responding -- stop counting "thinking" time.
            thinkingSinceRef.current = null;
          }
        });

        room.on(
          RoomEvent.TrackSubscribed,
          (track: RemoteTrack, _publication, _participant: RemoteParticipant) => {
            if (track.kind !== Track.Kind.Audio) return;
            agentAnalyserRef.current?.dispose();
            agentAnalyserRef.current = new LevelAnalyser(track.mediaStreamTrack);
            const el = track.attach();
            el.autoplay = true;
            document.body.appendChild(el);
            mediaElRef.current = el;
          },
        );

        room.on(RoomEvent.TrackUnsubscribed, (track: RemoteTrack) => {
          if (track.kind !== Track.Kind.Audio) return;
          for (const el of track.detach()) el.remove();
          agentAnalyserRef.current?.dispose();
          agentAnalyserRef.current = null;
        });

        room.on(RoomEvent.Disconnected, () => setStatusSafe("disconnected"));
        room.on(RoomEvent.Reconnecting, () => setStatusSafe("connecting"));
        room.on(RoomEvent.Reconnected, () => setStatusSafe("idle"));

        // Scenario progress arrives as a text data stream (same mechanism as
        // transcription), not the older publish_data()/"dataReceived" packet
        // API -- that legacy path was never actually delivered in testing.
        room.registerTextStreamHandler(SCENARIO_PROGRESS_TOPIC, async (reader: TextStreamReader) => {
          try {
            const text = await reader.readAll();
            applyScenarioEvent(JSON.parse(text) as ScenarioProgressEvent);
          } catch {
            // ignore malformed payloads
          }
        });

        await room.connect(url, token);

        // A denied/missing microphone shouldn't strand the whole session --
        // the agent's voice and the transcript still work either way.
        try {
          await room.localParticipant.setMicrophoneEnabled(true);
          const micPub = room.localParticipant.getTrackPublication(Track.Source.Microphone);
          if (micPub?.track) {
            micAnalyserRef.current = new LevelAnalyser(micPub.track.mediaStreamTrack);
          }
        } catch (micErr) {
          setError(
            `Микрофон недоступен: ${micErr instanceof Error ? micErr.message : String(micErr)}`,
          );
        }

        setStatusSafe("idle");

        const loop = () => {
          const mic = micAnalyserRef.current?.read() ?? 0;
          const agent = agentAnalyserRef.current?.read() ?? 0;
          micLevelRef.current = mic;
          agentLevelRef.current = agent;

          if (agent > SPEAK_THRESHOLD) {
            setStatusSafe("speaking");
            thinkingSinceRef.current = null;
          } else if (mic > LISTEN_THRESHOLD) {
            setStatusSafe("listening");
            thinkingSinceRef.current = null;
          } else if (thinkingSinceRef.current !== null) {
            if (Date.now() - thinkingSinceRef.current > THINKING_TIMEOUT_MS) {
              thinkingSinceRef.current = null;
              setStatusSafe("idle");
            } else {
              setStatusSafe("thinking");
            }
          } else {
            setStatusSafe("idle");
          }

          rafId = requestAnimationFrame(loop);
        };
        rafId = requestAnimationFrame(loop);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    }

    void connect();

    return () => {
      cancelled = true;
      cancelAnimationFrame(rafId);
      micAnalyserRef.current?.dispose();
      agentAnalyserRef.current?.dispose();
      mediaElRef.current?.remove();
      roomRef.current?.disconnect();
      roomRef.current = null;
    };
  }, [identity, setStatusSafe, upsertTranscriptLine, applyScenarioEvent]);

  // A final user line with no agent reply yet means the agent is thinking.
  useEffect(() => {
    const last = transcript[transcript.length - 1];
    if (last && last.role === "user" && last.final) {
      thinkingSinceRef.current = Date.now();
    }
  }, [transcript]);

  const sendText = useCallback(async (text: string) => {
    const room = roomRef.current;
    if (!room) return;
    await room.localParticipant.sendText(text, { topic: "lk.chat" });
  }, []);

  return { status, transcript, error, micLevelRef, agentLevelRef, sendText, runningScenario };
}
