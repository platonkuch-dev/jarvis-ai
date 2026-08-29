/**
 * The bridge to native-core: spawns the Rust binary once, keeps it alive as
 * a long-running child process, and speaks the line-delimited JSON-RPC
 * protocol (protocol.ts) over its stdin/stdout. One process per orchestrator
 * instance — much cheaper than a process-per-call, and it's what lets
 * native-core log a coherent "[APP][WINDOW][ACTION]..." trail per session.
 */
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface } from "node:readline";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

import { JarvisError, isRpcResponse, type RpcResponse } from "./protocol.ts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

interface PendingCall {
  resolve: (value: unknown) => void;
  reject: (err: JarvisError) => void;
  timer: ReturnType<typeof setTimeout>;
  method: string;
}

/** Locates native-core.exe next to this package — release build preferred. */
export function resolveNativeCoreBinary(): string {
  const root = path.resolve(__dirname, "..", "..", "native-core", "target");
  const candidates = [
    path.join(root, "release", "native-core.exe"),
    path.join(root, "debug", "native-core.exe"),
  ];
  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate;
  }
  throw new JarvisError({
    code: "NATIVE_CORE_NOT_BUILT",
    message: `native-core.exe not found. Build it first: cd native-core && cargo build (looked in: ${candidates.join(", ")})`,
    source: "rustBridge",
    recoverable: false,
  });
}

export class RustBridge {
  private readonly binaryPath: string;
  private child: ChildProcessWithoutNullStreams | null = null;
  private pending: Map<string, PendingCall> = new Map();
  private nextId = 1;
  private startupError: JarvisError | null = null;

  constructor(binaryPath?: string) {
    this.binaryPath = binaryPath ?? resolveNativeCoreBinary();
  }

  start(): void {
    if (this.child) return;

    const child = spawn(this.binaryPath, [], { stdio: ["pipe", "pipe", "pipe"] });
    this.child = child;

    const rl = createInterface({ input: child.stdout });
    rl.on("line", (line) => this.handleLine(line));

    child.stderr.setEncoding("utf-8");
    child.stderr.on("data", (chunk: string) => {
      // native-core's own diagnostics — surfaced, never silently dropped.
      // native-core already labels its own lines with "[native-core] ",
      // so this is a plain passthrough rather than a second prefix.
      for (const l of chunk.split("\n")) {
        if (l.trim()) process.stderr.write(`${l}\n`);
      }
    });

    child.on("exit", (code, signal) => {
      const err = new JarvisError({
        code: "NATIVE_CORE_EXITED",
        message: `native-core exited (code=${code}, signal=${signal}) while ${this.pending.size} call(s) were pending.`,
        source: "rustBridge",
        recoverable: false,
      });
      for (const [, call] of this.pending) {
        clearTimeout(call.timer);
        call.reject(err);
      }
      this.pending.clear();
      this.child = null;
    });

    child.on("error", (err) => {
      this.startupError = new JarvisError({
        code: "NATIVE_CORE_SPAWN_FAILED",
        message: `Failed to spawn native-core: ${err.message}`,
        source: "rustBridge",
        recoverable: false,
      });
    });
  }

  private handleLine(line: string): void {
    const trimmed = line.trim();
    if (!trimmed) return;

    let parsed: unknown;
    try {
      parsed = JSON.parse(trimmed);
    } catch (e) {
      console.error(`[rustBridge] Malformed line from native-core, ignoring: ${trimmed}`);
      return;
    }
    if (!isRpcResponse(parsed)) {
      console.error(`[rustBridge] Unrecognized message shape from native-core: ${trimmed}`);
      return;
    }
    const response: RpcResponse = parsed;
    const call = this.pending.get(response.id);
    if (!call) {
      // Late/duplicate response after a timeout already resolved — log, don't crash.
      console.error(`[rustBridge] Response for unknown/expired id '${response.id}'`);
      return;
    }
    this.pending.delete(response.id);
    clearTimeout(call.timer);

    if (response.success) {
      call.resolve(response.result);
    } else {
      call.reject(JarvisError.fromRpcError(response.error));
    }
  }

  /** Send one request, wait for its matching response (or time out). */
  async call(method: string, params: Record<string, unknown> = {}, timeoutMs = 15000): Promise<unknown> {
    if (this.startupError) throw this.startupError;
    if (!this.child) this.start();
    const child = this.child;
    if (!child) throw new JarvisError({ code: "NATIVE_CORE_NOT_RUNNING", message: "native-core is not running.", source: "rustBridge", recoverable: true });

    const id = `${Date.now()}-${this.nextId++}`;
    const request = { id, method, params };

    return new Promise<unknown>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new JarvisError({
          code: "NATIVE_CORE_TIMEOUT",
          message: `Call to '${method}' timed out after ${timeoutMs}ms.`,
          source: "rustBridge",
          recoverable: true,
        }));
      }, timeoutMs);

      this.pending.set(id, { resolve, reject, timer, method });

      child.stdin.write(JSON.stringify(request) + "\n", (err) => {
        if (err) {
          this.pending.delete(id);
          clearTimeout(timer);
          reject(new JarvisError({ code: "NATIVE_CORE_WRITE_FAILED", message: err.message, source: "rustBridge", recoverable: true }));
        }
      });
    });
  }

  stop(): void {
    if (this.child) {
      this.child.stdin.end();
      this.child.kill();
      this.child = null;
    }
  }
}
