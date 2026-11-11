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

  it("with auth off, where list_mine is never read, shows on an empty namespace alone", () => {
    expect(shouldShowGettingStarted({ mine: null, memoryTotal: 0, authOff: true })).toBe(true);
    expect(shouldShowGettingStarted({ mine: null, memoryTotal: 3, authOff: true })).toBe(false);
    expect(shouldShowGettingStarted({ mine: null, memoryTotal: null, authOff: true })).toBe(false);
  });
});

describe("onboarding persistence", () => {
  afterEach(() => localStorage.clear());

  it("round-trips ticks, started and dismissed under a per-subject, per-namespace versioned key", () => {
    saveOnboarding("a@x", "ns1", { done: { connect: true }, started: true, dismissed: false, finished: false });
    expect(onboardingKey("a@x", "ns1")).toBe("loom_getting_started_v2:a@x:ns1");
    expect(loadOnboarding("a@x", "ns1")).toEqual({
      done: { connect: true },
      started: true,
      dismissed: false,
      finished: false,
    });
    expect(loadOnboarding("a@x", "ns2")).toEqual(EMPTY_ONBOARDING);
    expect(loadOnboarding("b@x", "ns1")).toEqual(EMPTY_ONBOARDING);
    expect(countDone(loadOnboarding("a@x", "ns1").done)).toBe(1);
  });

  it("reads every step done on an earlier visit as finished, and never stores the flag", () => {
    saveOnboarding("a@x", "ns1", {
      done: { connect: true, write: true },
      started: true,
      dismissed: false,
      finished: true,
    });
    expect(JSON.parse(localStorage.getItem(onboardingKey("a@x", "ns1")) ?? "{}")).not.toHaveProperty("finished");
    expect(loadOnboarding("a@x", "ns1").finished).toBe(true);
  });

  it("treats a corrupt or foreign value as empty instead of throwing", () => {
    localStorage.setItem(onboardingKey("c@x", "n"), "{not json");
    expect(loadOnboarding("c@x", "n")).toEqual(EMPTY_ONBOARDING);
    localStorage.setItem(
      onboardingKey("d@x", "n"),
      JSON.stringify({ done: { bogus: true, write: "yes" }, dismissed: "no" }),
    );
    expect(loadOnboarding("d@x", "n")).toEqual(EMPTY_ONBOARDING);
  });

  it.each(["null", "5", '"done"', "[]", '{"done":5}', '{"done":null}', '{"done":["connect"]}'])(
    "reads the malformed stored value %s as fresh state",
    (stored) => {
      localStorage.setItem(onboardingKey("e@x", "n"), stored);
      expect(loadOnboarding("e@x", "n")).toEqual(EMPTY_ONBOARDING);
    },
  );

  it("keeps the valid fields of a partly malformed value", () => {
    localStorage.setItem(onboardingKey("f@x", "n"), JSON.stringify({ done: 5, started: true, dismissed: true }));
    expect(loadOnboarding("f@x", "n")).toEqual({ done: {}, started: true, dismissed: true, finished: false });
  });

  it("keeps one subject's ticks apart in two orgs with a namespace of the same name", () => {
    saveOnboarding("a@x", "org-a/team", { done: {}, started: true, dismissed: true, finished: false });
    expect(loadOnboarding("a@x", "org-a/team").dismissed).toBe(true);
    expect(loadOnboarding("a@x", "org-b/team").dismissed).toBe(false);
  });
});
