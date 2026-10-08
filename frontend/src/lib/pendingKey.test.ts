import { describe, expect, it } from "vitest";
import { pendingKey } from "./pendingKey";

// The key is persisted in users' browsers: these literals are the format as shipped.
describe("pendingKey", () => {
  it("keys on the subject when there is one", () => {
    expect(pendingKey("alice", "acme")).toBe("loom_pending_v1_alice");
  });
  it("falls back to the org for an empty or missing subject", () => {
    expect(pendingKey("", "acme")).toBe("loom_pending_v1_acme");
    expect(pendingKey(undefined, "acme")).toBe("loom_pending_v1_acme");
    expect(pendingKey(undefined, "dev")).toBe("loom_pending_v1_dev");
  });
});
