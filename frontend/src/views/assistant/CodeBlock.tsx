/** Text shown as written, wrapping and keeping its line breaks: a draft's current and
 *  proposed text, a call's arguments, the operator extraction policy. Settings' snippet look. */
export function CodeBlock({ side, testId, children }: { side?: string; testId?: string; children: string }) {
  return (
    <pre
      className="m-0 text-xs font-mono bg-surface-2 rounded-lg p-3 overflow-auto whitespace-pre-wrap break-words"
      data-side={side}
      data-testid={testId}
    >
      {children}
    </pre>
  );
}
