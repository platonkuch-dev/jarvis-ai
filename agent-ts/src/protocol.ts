/**
 * IPC wire format shared with native-core (Rust) — see
 * native-core/src/protocol.rs and shared/schemas/ipc-protocol.md for the
 * authoritative description. Kept in sync by hand; the integration test in
 * tests/rustBridge.integration.test.ts is what actually catches drift.
 */

export interface RpcRequest {
  id: string;
  method: string;
  params: Record<string, unknown>;
}

export interface RpcErrorInfo {
  code: string;
  message: string;
  source: string;
  details?: unknown;
  recoverable: boolean;
}

export interface RpcSuccessResponse {
  id: string;
  success: true;
  result: unknown;
}

export interface RpcErrorResponse {
  id: string;
  success: false;
  error: RpcErrorInfo;
}

export type RpcResponse = RpcSuccessResponse | RpcErrorResponse;

export function isRpcResponse(value: unknown): value is RpcResponse {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return typeof v["id"] === "string" && typeof v["success"] === "boolean";
}

/**
 * A structured, agent-recoverable error. Every layer of this codebase
 * (rustBridge, ToolRegistry, PermissionManager, Agent) throws or returns
 * this shape instead of a bare Error/string, per the "no empty catch,
 * errors carry code/message/source/details/recoverable" rule.
 */
export class JarvisError extends Error {
  readonly code: string;
  readonly source: string;
  readonly details?: unknown;
  readonly recoverable: boolean;

  constructor(params: {
    code: string;
    message: string;
    source: string;
    details?: unknown;
    recoverable: boolean;
  }) {
    super(params.message);
    this.name = "JarvisError";
    this.code = params.code;
    this.source = params.source;
    this.details = params.details;
    this.recoverable = params.recoverable;
  }

  static fromRpcError(err: RpcErrorInfo): JarvisError {
    return new JarvisError({
      code: err.code,
      message: err.message,
      source: err.source,
      details: err.details,
      recoverable: err.recoverable,
    });
  }

  toJSON(): Record<string, unknown> {
    return {
      code: this.code,
      message: this.message,
      source: this.source,
      details: this.details,
      recoverable: this.recoverable,
    };
  }
}
