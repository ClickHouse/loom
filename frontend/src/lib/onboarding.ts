import type { MyNamespaceEntry } from "../api";
import { isPersonalNamespace, isSelectableNamespace } from "./namespace";
import { isRecord } from "./guards";

/**
 * The first-run checklist on Memories: which steps exist, when it shows, and where its
 * ticks live.
 *
 * Ticks are per browser and per subject, org and namespace, under one versioned localStorage
 * key: Loom has no per-user preference store on the server (`loom.user_state` carries
 * `default_namespace` only). Every read and write is wrapped: a private window or a
 * blocked store degrades to "not persisted", never to a crash on the landing view.
 */
export const ONBOARDING_STEPS = ["connect", "write"] as const;
export type OnboardingStep = (typeof ONBOARDING_STEPS)[number];

export interface OnboardingState {
  done: Partial<Record<OnboardingStep, true>>;
  /** The namespace was new on a visit. The checklist then stays until hidden, so writing
   *  the first memory ticks step two instead of hiding the list. */
  started: boolean;
  /** "Hide" — the checklist stays hidden for this subject, org and namespace. */
  dismissed: boolean;
  /** Every step was done on an earlier visit: this visit does not show it again. */
  finished: boolean;
}

const KEY_PREFIX = "loom_getting_started_v2";

/** `place` is `org/namespace`: one subject in two orgs keeps each one's ticks. */
export function onboardingKey(subject: string, place: string): string {
  return `${KEY_PREFIX}:${subject || "-"}:${place || "-"}`;
}

export const EMPTY_ONBOARDING: OnboardingState = { done: {}, started: false, dismissed: false, finished: false };

export function loadOnboarding(subject: string, place: string): OnboardingState {
  try {
    const raw = localStorage.getItem(onboardingKey(subject, place));
    if (!raw) return EMPTY_ONBOARDING;
    const parsed: unknown = JSON.parse(raw);
    if (!isRecord(parsed)) return EMPTY_ONBOARDING;
    const stored = isRecord(parsed.done) ? parsed.done : {};
    const done: OnboardingState["done"] = {};
    for (const step of ONBOARDING_STEPS) {
      if (stored[step] === true) done[step] = true;
    }
    return {
      done,
      started: parsed.started === true,
      dismissed: parsed.dismissed === true,
      // Read once at load: finishing during this visit keeps "All set" on screen.
      finished: countDone(done) === ONBOARDING_STEPS.length,
    };
  } catch {
    return EMPTY_ONBOARDING;
  }
}

export function saveOnboarding(subject: string, place: string, state: OnboardingState): void {
  try {
    const { finished: _readAtLoad, ...kept } = state;
    localStorage.setItem(onboardingKey(subject, place), JSON.stringify(kept));
  } catch {
    /* quota, private mode, or storage disabled: the session keeps its in-memory ticks */
  }
}

/**
 * A "new user" for the checklist: the current namespace holds no memories, AND (with
 * auth on) `list_mine` shows only the auto-claimed personal namespace, or nothing at all.
 * With auth off the server serves one namespace and `list_mine` is never read, so an
 * empty namespace is enough. Both inputs are `null` while unknown — a failed or
 * unfinished read never shows a checklist to someone who is not new.
 */
export function shouldShowGettingStarted(args: {
  mine: readonly MyNamespaceEntry[] | null;
  memoryTotal: number | null;
  authOff?: boolean;
}): boolean {
  if (args.memoryTotal === null) return false;
  if (args.memoryTotal > 0) return false;
  if (args.authOff) return true;
  if (args.mine === null) return false;
  const others = args.mine.filter(
    (m) => isSelectableNamespace(m) && (m.status ?? "active") === "active" && !isPersonalNamespace(m.namespace),
  );
  return others.length === 0;
}

export function countDone(done: OnboardingState["done"]): number {
  return ONBOARDING_STEPS.filter((s) => done[s]).length;
}
