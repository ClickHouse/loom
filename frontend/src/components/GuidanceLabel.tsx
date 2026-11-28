import type { SearchHit } from "../api";

export function GuidanceLabel({ hit, reasons = false }: { hit: SearchHit; reasons?: boolean }) {
  const guidance = hit.use_guidance;
  if (!guidance) return null;
  return (
    <span className="block text-xs text-text-muted">
      Suggested use: {guidance.next_step.replaceAll("_", " ")}
      {reasons && guidance.reasons.length > 0 && (
        <span className="block">{guidance.reasons.map((reason) => reason.replaceAll("_", " ")).join(", ")}</span>
      )}
    </span>
  );
}
