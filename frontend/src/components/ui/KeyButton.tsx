import { Link } from "@clickhouse/click-ui/Link";

/** A memory key that opens the memory in the inspector: the audit's samples, the
 *  Assistant's inspected rows and a draft's cited rows. Click UI's Link as an action, the
 *  key in mono. */
export function KeyButton({ memoryKey, onInspect }: { memoryKey: string; onInspect: (key: string) => void }) {
  return (
    <Link
      component="button"
      type="button"
      size="sm"
      className="min-h-9 max-w-full pointer-coarse:min-h-11"
      onClick={() => onInspect(memoryKey)}
      aria-label={`Open memory ${memoryKey}`}
    >
      <span className="font-mono text-xs break-all text-left">{memoryKey}</span>
    </Link>
  );
}
