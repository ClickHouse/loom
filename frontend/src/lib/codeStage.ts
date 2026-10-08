import type { CodeRepoSummary } from "../api";
import { fmtInt } from "./format";
import { isRecord } from "./guards";

type StageTone = "ok" | "warn" | "bad" | "muted";
interface StageLabel {
  label: string;
  tone: StageTone;
  title: string;
}

function parseDetail(r: CodeRepoSummary): Record<string, unknown> {
  if (!r.stage_detail) return {};
  try {
    const v: unknown = JSON.parse(r.stage_detail);
    return isRecord(v) ? v : {};
  } catch {
    return {};
  }
}

/** Whole-percent coverage: the row's own ratio first, else the detail the worker wrote. */
function embedPercent(r: CodeRepoSummary, detail: Record<string, unknown> = parseDetail(r)): number {
  const cov =
    typeof r.embedding_coverage === "number" && (r.chunks_total ?? 0) > 0
      ? r.embedding_coverage
      : typeof detail.embedding_coverage === "number"
        ? detail.embedding_coverage
        : 0;
  return Math.max(0, Math.min(100, Math.floor(cov * 100)));
}

/** A self-service entry's first index, as one label. indexing → "indexing 1,200 of 52,000
 *  · ~38 min left"; embedding → "embedding 41%"; ready → null (the freshness pill takes
 *  over). */
export function stageLabel(r: CodeRepoSummary): StageLabel | null {
  const st = r.stage || "";
  if (!st || st === "ready") {
    // An operator-indexed namespace (`loom code backfill` without --embed) has no
    // bootstrap stage, but its vector leg can still be filling — the same pill, from the
    // server's verdict on the row's coverage.
    if (r.searchable === false) {
      const pct = embedPercent(r);
      return {
        label: `embedding ${pct}%`,
        tone: "warn",
        title: `Indexed; the vector leg is ${pct}% filled. Search is lexical-only until it reaches the ready threshold.`,
      };
    }
    return null;
  }
  const detail = parseDetail(r);
  if (st === "indexing") {
    const done = Number(detail.done ?? 0),
      total = Number(detail.total ?? 0);
    const eta = typeof detail.eta === "string" && detail.eta ? ` · ${detail.eta}` : "";
    const rate = detail.files_per_s ? ` at ${detail.files_per_s} files/s` : "";
    return {
      label: `indexing ${fmtInt(done)} of ${fmtInt(total)}${eta}`,
      tone: "warn",
      title: `First index in progress${rate}. Search works on what is indexed so far, lexical-only until embeddings finish.`,
    };
  }
  if (st === "embedding") {
    const pct = embedPercent(r, detail);
    return {
      label: `embedding ${pct}%`,
      tone: "warn",
      title: `Indexed; the vector leg is ${pct}% filled. Search is lexical-only until enough chunks carry an embedding.`,
    };
  }
  if (st === "cloning")
    return {
      label: "cloning",
      tone: "warn",
      title: "Fetching the repository's history. A large repository takes a few minutes.",
    };
  if (st === "queued")
    return { label: "queued", tone: "muted", title: "The indexer picks new repositories up within two minutes." };
  if (st === "error")
    return {
      label: "error",
      tone: "bad",
      title: String(
        detail.reason || r.stage_detail || "The indexer could not fetch this repository; it retries every pass.",
      ),
    };
  return { label: st, tone: "muted", title: "" };
}

interface StageFlip {
  namespace: string;
  repo: string;
  branch: string;
  to: "ready" | "error";
  reason: string;
}

/** The entries whose stage moved to ready or error between two polls — what the page
 *  announces (no generic bell feed exists yet). */
export function stageFlips(prev: CodeRepoSummary[] | null, next: CodeRepoSummary[]): StageFlip[] {
  if (!prev) return [];
  const before = new Map(prev.map((r) => [r.namespace, r.stage || ""]));
  const out: StageFlip[] = [];
  for (const r of next) {
    const was = before.get(r.namespace);
    if (was === undefined) continue;
    const now = r.stage || "";
    if (was === now) continue;
    const building = was === "queued" || was === "cloning" || was === "indexing" || was === "embedding";
    if (now === "ready" && building)
      out.push({ namespace: r.namespace, repo: r.repo, branch: r.branch, to: "ready", reason: "" });
    else if (now === "error" && was !== "error")
      out.push({
        namespace: r.namespace,
        repo: r.repo,
        branch: r.branch,
        to: "error",
        reason: String(parseDetail(r).reason || ""),
      });
  }
  return out;
}
