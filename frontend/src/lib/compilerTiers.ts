/**
 * The Health tab's compiler-tier cards come from the status row's `compiler_tiers` — one
 * per producer the repository runs — instead of a hard-coded C++ card and a Python card.
 * A TypeScript repository shows its scip-typescript tier (watermark `scip_commit_ts`); a
 * C++ tree with a Python tier shows both; an entry whose first compiler run has not
 * happened shows its configured tier as unset. An entry with no tier gets one "not
 * configured" card, never a wrong one.
 */
import type { CodeIndexStatus } from "../api";

export type TierIcon = "cpp" | "typescript" | "go" | "python" | "none";

export interface TierCard {
  key: string;
  title: string;
  icon: TierIcon;
  watermark: string;
  lag: number;
  updatedAt?: string;
  workers: string[];
  note: string;
}

const ICONS: Record<string, TierIcon> = { clang: "cpp", typescript: "typescript", go: "go", python: "python" };

export function tierCards(st: Pick<CodeIndexStatus, "compiler_tiers" | "state">): TierCard[] {
  const tiers = st.compiler_tiers;
  if (tiers.length === 0) {
    return [
      {
        key: "none",
        title: "Compiler tier · not configured",
        icon: "none",
        watermark: "",
        lag: -1,
        workers: [],
        note: "No scip_producer on this entry and no compiler watermark yet. The tree's manifests (go.mod, tsconfig.json, CMakeLists.txt, pyproject.toml) decide which compiler worker owns it.",
      },
    ];
  }
  return tiers.map((t) => ({
    key: t.watermark_key,
    title: `Compiler tier · ${t.label} (${t.tool})${t.primary ? "" : " · also indexed"}`,
    icon: ICONS[t.producer] ?? "none",
    watermark: t.commit,
    lag: t.lag,
    // the server reads updated_at off the same state row; fall back to it for
    // a status whose tier row predates the field
    updatedAt: t.updated_at || st.state?.[t.watermark_key]?.updated_at || undefined,
    workers: t.workers,
    note: t.note,
  }));
}
