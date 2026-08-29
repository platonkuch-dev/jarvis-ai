/**
 * Every tool is classified once, at registration. Claude never gets to pick
 * its own risk level for an action — that would defeat the point.
 *
 *   SAFE       — read-only or trivially reversible (list windows, read a field)
 *   NORMAL     — routine, expected, reversible (open/close/focus an app, type text)
 *   SENSITIVE  — harder to reverse or affects more than the target app
 *                (force-close, kill a process, move/resize windows in bulk)
 *   DANGEROUS  — explicitly gated: never runs without a confirmation the
 *                caller must supply (shutdown, mass file deletion, disk ops —
 *                none of which are wired into this Windows-control slice,
 *                but the category exists so future tools have somewhere to go)
 *
 * Matches the spirit of windows_control's PROTECTED_PROCESS_NAMES guard on
 * the Python/Rust side — this is the layer *above* that: policy, not a
 * hardcoded denylist.
 */

export type PermissionLevel = "SAFE" | "NORMAL" | "SENSITIVE" | "DANGEROUS";

export interface PermissionDecision {
  allowed: boolean;
  level: PermissionLevel;
  reason: string;
}

export interface PermissionCheckInput {
  toolName: string;
  level: PermissionLevel;
  /** Present when the caller is re-invoking after an explicit user confirmation. */
  confirmed?: boolean;
}

export class PermissionManager {
  /**
   * SAFE/NORMAL/SENSITIVE run autonomously — per the project's explicit
   * "don't ask for confirmation on ordinary safe actions" directive.
   * DANGEROUS always requires `confirmed: true` on the call, no exceptions.
   */
  check(input: PermissionCheckInput): PermissionDecision {
    if (input.level !== "DANGEROUS") {
      return { allowed: true, level: input.level, reason: "Autonomous execution permitted for this risk level." };
    }
    if (input.confirmed) {
      return { allowed: true, level: input.level, reason: "Explicit confirmation supplied for a dangerous action." };
    }
    return {
      allowed: false,
      level: input.level,
      reason: `'${input.toolName}' is classified DANGEROUS and requires explicit confirmation before it will run.`,
    };
  }
}
