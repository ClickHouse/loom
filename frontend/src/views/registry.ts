// The view chunks by name, so boot can start the download for the view the URL names
// before any network answer arrives. React.lazy in App.tsx fetches a chunk when the view
// first renders, which is after whoami and list_mine; without this a deep link pays that
// chain and then the chunk. The same import() here resolves to the same module, so the lazy
// boundary finds it loading or loaded.
import type { View } from "../lib/state";

export const VIEW_LOADERS: Record<View, () => Promise<unknown>> = {
  memories: () => import("./Recent"),
  recall: () => import("./RecallLab"),
  profile: () => import("./Profile"),
  knowledge: () => import("./Documents"),
  sharing: () => import("./Sharing"),
  sessions: () => import("./Conversations"),
  runs: () => import("./Runs"),
  graph: () => import("./Graph"),
  assistant: () => import("./Assistant"),
  help: () => import("./Help"),
  browse: () => import("./Browse"),
  code: () => import("./Code"),
  tasks: () => import("./Tasks"),
  setup: () => import("./Setup"),
  "admin:review_queue": () => import("./admin/ReviewQueue"),
  "admin:health": () => import("./admin/Health"),
  "admin:guardrails": () => import("./admin/Blacklist"),
  "admin:policies": () => import("./admin/Policies"),
  "admin:activity": () => import("./admin/Audit"),
  "admin:usage": () => import("./admin/Spending"),
  "admin:agents": () => import("./admin/Agents"),
  "admin:users": () => import("./admin/Users"),
  "admin:settings": () => import("./admin/DeploymentSettings"),
};

/** Start loading a view's chunk; failures are the lazy boundary's to report. */
export function preloadView(view: View | null | undefined): void {
  if (!view) return;
  const load = VIEW_LOADERS[view];
  if (load)
    void load().catch(() => {
      /* the Suspense boundary surfaces a failed chunk */
    });
}
