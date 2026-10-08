import type { MyNamespaceEntry } from "../api";
import { isPersonalNamespace, isSelectableNamespace } from "./namespace";
import { isRecord } from "./guards";

/**
 * The first-visit checklist: which steps exist, when the checklist shows, and where its
 * ticks live.
 *
 * Ticks are per browser and per subject, under one versioned localStorage
 * key. Loom has no per-user preference store on the server yet
 * (`loom.user_state` carries `default_namespace` only, and a new column is a migration) — cross-device persistence is future work. Every
 * read and write is wrapped: a private window or a blocked store degrades to "not
 * persisted", never to a crash on the landing view.
 */
export const ONBOARDING_STEPS = ["connect", "write", "find", "invite"] as const;
export type OnboardingStep = (typeof ONBOARDING_STEPS)[number];

export interface OnboardingState {
  done: Partial<Record<OnboardingStep, true>>;
  /** The checklist has been shown once. It then stays until dismissed, so
   *  writing the first memory ticks step two instead of hiding the list. */
  started: boolean;
  /** "Skip the tour" — the checklist stays hidden for this subject. */
  dismissed: boolean;
}

const KEY_PREFIX = "loom_getting_started_v1";

export function onboardingKey(subject: string): string {
  return `${KEY_PREFIX}:${subject || "-"}`;
}

export const EMPTY_ONBOARDING: OnboardingState = { done: {}, started: false, dismissed: false };

export function loadOnboarding(subject: string): OnboardingState {
  try {
    const raw = localStorage.getItem(onboardingKey(subject));
    if (!raw) return EMPTY_ONBOARDING;
    const parsed: unknown = JSON.parse(raw);
    if (!isRecord(parsed)) return EMPTY_ONBOARDING;
    const stored = isRecord(parsed.done) ? parsed.done : {};
    const done: OnboardingState["done"] = {};
    for (const step of ONBOARDING_STEPS) {
      if (stored[step] === true) done[step] = true;
    }
    return { done, started: parsed.started === true, dismissed: parsed.dismissed === true };
  } catch {
    return EMPTY_ONBOARDING;
  }
}

export function saveOnboarding(subject: string, state: OnboardingState): void {
  try {
    localStorage.setItem(onboardingKey(subject), JSON.stringify(state));
  } catch {
    /* quota, private mode, or storage disabled: the session keeps its in-memory ticks */
  }
}

/**
 * A "new user" for the checklist: `list_mine` shows only the auto-claimed personal
 * namespace (or nothing at all), AND the current namespace holds no memories. Both inputs
 * are `null` while unknown — a failed or unfinished read never shows a tour to someone
 * who is not new.
 */
export function shouldShowGettingStarted(args: {
  mine: readonly MyNamespaceEntry[] | null;
  memoryTotal: number | null;
}): boolean {
  if (args.mine === null || args.memoryTotal === null) return false;
  if (args.memoryTotal > 0) return false;
  const others = args.mine.filter(
    (m) => isSelectableNamespace(m) && (m.status ?? "active") === "active" && !isPersonalNamespace(m.namespace),
  );
  return others.length === 0;
}

export function countDone(done: OnboardingState["done"]): number {
  return ONBOARDING_STEPS.filter((s) => done[s]).length;
}
