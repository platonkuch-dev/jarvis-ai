import { describe, it, expect } from "vitest";
import { PermissionManager } from "../src/PermissionManager.ts";

describe("PermissionManager", () => {
  const pm = new PermissionManager();

  it("allows SAFE/NORMAL/SENSITIVE tools autonomously, no confirmation needed", () => {
    for (const level of ["SAFE", "NORMAL", "SENSITIVE"] as const) {
      const decision = pm.check({ toolName: "some_tool", level });
      expect(decision.allowed).toBe(true);
    }
  });

  it("refuses a DANGEROUS tool without explicit confirmation", () => {
    const decision = pm.check({ toolName: "shutdown_computer", level: "DANGEROUS" });
    expect(decision.allowed).toBe(false);
    expect(decision.reason).toContain("DANGEROUS");
  });

  it("allows a DANGEROUS tool once explicitly confirmed", () => {
    const decision = pm.check({ toolName: "shutdown_computer", level: "DANGEROUS", confirmed: true });
    expect(decision.allowed).toBe(true);
  });
});
