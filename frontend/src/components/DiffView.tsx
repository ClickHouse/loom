interface DiffLine {
  kind: "same" | "add" | "del";
  text: string;
}

function diffLines(a: string, b: string): DiffLine[] {
  const aLines = (a || "").split("\n");
  const bLines = (b || "").split("\n");

  // Classic Myers-style DP over line arrays. For small values (<200 lines)
  // this is fast enough; for larger we'd want a proper Myers diff.
  const m = aLines.length;
  const n = bLines.length;

  // LCS table
  const dp: number[][] = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      if (aLines[i] === bLines[j]) dp[i][j] = dp[i + 1][j + 1] + 1;
      else dp[i][j] = Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }

  // Walk back to construct diff
  const out: DiffLine[] = [];
  let i = 0,
    j = 0;
  while (i < m && j < n) {
    if (aLines[i] === bLines[j]) {
      out.push({ kind: "same", text: aLines[i] });
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push({ kind: "del", text: aLines[i] });
      i++;
    } else {
      out.push({ kind: "add", text: bLines[j] });
      j++;
    }
  }
  while (i < m) {
    out.push({ kind: "del", text: aLines[i++] });
  }
  while (j < n) {
    out.push({ kind: "add", text: bLines[j++] });
  }
  return out;
}

interface DiffViewProps {
  prev: string;
  next: string;
  prevLabel: string;
  nextLabel: string;
}

export function DiffView({ prev, next, prevLabel, nextLabel }: DiffViewProps) {
  const lines = diffLines(prev, next);
  if (prev === next) {
    return (
      <div className="text-sm text-text-muted italic px-2 py-3">No textual difference between these versions.</div>
    );
  }
  return (
    <div className="rounded-sm border border-border bg-surface-2 overflow-hidden font-mono text-xs">
      <div className="flex items-center justify-between px-3 py-1.5 border-b border-border bg-surface-3 text-xs text-text-muted tabular">
        <span>− {prevLabel}</span>
        <span>+ {nextLabel}</span>
      </div>
      <div className="px-3 py-2 max-h-[280px] overflow-auto whitespace-pre-wrap">
        {lines.map((l, idx) => {
          const cls =
            l.kind === "add"
              ? "bg-success/15 text-success"
              : l.kind === "del"
                ? "bg-danger/15 text-danger"
                : "text-text";
          const marker = l.kind === "add" ? "+" : l.kind === "del" ? "−" : " ";
          return (
            <div key={idx} className={cls}>
              <span className="select-none text-text-muted pr-2">{marker}</span>
              {l.text || " "}
            </div>
          );
        })}
      </div>
    </div>
  );
}
