import { afterEach, describe, expect, it } from "vitest";
import type { MyNamespaceEntry } from "../api";
import {
  countDone,
  EMPTY_ONBOARDING,
  loadOnboarding,
  onboardingKey,
  saveOnboarding,
  shouldShowGettingStarted,
} from "./onboarding";

// The checklist shows for an account whose list_mine holds only the auto-claimed personal
// namespace (or none) and whose current namespace holds no memories; unknown inputs never
// show it.

const row = (namespace: string, extra: Partial<MyNamespaceEntry> = {}): MyNamespaceEntry => ({
  namespace,
  role: "admin",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
  ...extra,
});

describe("shouldShowGettingStarted", () => {
  it("shows for the personal namespace alone with zero memories, and for no namespaces at all", () => {
    expect(shouldShowGettingStarted({ mine: [row("personal-a")], memoryTotal: 0 })).toBe(true);
    expect(shouldShowGettingStarted({ mine: [], memoryTotal: 0 })).toBe(true);
  });

  it("hides once the namespace holds a memory", () => {
    expect(shouldShowGettingStarted({ mine: [row("personal-a")], memoryTotal: 1 })).toBe(false);
  });

  it("hides for an account that already created or joined another space", () => {
    expect(
      shouldShowGettingStarted({ mine: [row("personal-a"), row("team-x", { role: "writer" })], memoryTotal: 0 }),
    ).toBe(false);
  });

  it("ignores repositories, wildcards and pending requests when counting other spaces", () => {
    const mine = [
      row("personal-a"),
      row("repo-1", { kind: "code" }),
      row("*", { role: "jwt" }),
      row("team-y", { status: "pending", role: "viewer" }),
    ];
    expect(shouldShowGettingStarted({ mine, memoryTotal: 0 })).toBe(true);
  });

  it("never shows while either input is unknown", () => {
    expect(shouldShowGettingStarted({ mine: null, memoryTotal: 0 })).toBe(false);
    expect(shouldShowGettingStarted({ mine: [row("personal-a")], memoryTotal: null })).toBe(false);
  });
});

describe("onboarding persistence", () => {
  afterEach(() => localStorage.clear());

  it("round-trips ticks, started and dismissed under a per-subject versioned key", () => {
    saveOnboarding("a@x", { done: { connect: true, find: true }, started: true, dismissed: false });
    expect(onboardingKey("a@x")).toBe("loom_getting_started_v1:a@x");
    expect(loadOnboarding("a@x")).toEqual({ done: { connect: true, find: true }, started: true, dismissed: false });
    expect(loadOnboarding("b@x")).toEqual(EMPTY_ONBOARDING);
    expect(countDone(loadOnboarding("a@x").done)).toBe(2);
  });

  it("treats a corrupt or foreign value as empty instead of throwing", () => {
    localStorage.setItem(onboardingKey("c@x"), "{not json");
    expect(loadOnboarding("c@x")).toEqual(EMPTY_ONBOARDING);
    localStorage.setItem(
      onboardingKey("d@x"),
      JSON.stringify({ done: { bogus: true, write: "yes" }, dismissed: "no" }),
    );
    expect(loadOnboarding("d@x")).toEqual(EMPTY_ONBOARDING);
  });

  it.each(["null", "5", '"done"', "[]", '{"done":5}', '{"done":null}', '{"done":["connect"]}'])(
    "reads the malformed stored value %s as fresh state",
    (stored) => {
      localStorage.setItem(onboardingKey("e@x"), stored);
      expect(loadOnboarding("e@x")).toEqual(EMPTY_ONBOARDING);
    },
  );

  it("keeps the valid fields of a partly malformed value", () => {
    localStorage.setItem(onboardingKey("f@x"), JSON.stringify({ done: 5, started: true, dismissed: true }));
    expect(loadOnboarding("f@x")).toEqual({ done: {}, started: true, dismissed: true });
  });
});
