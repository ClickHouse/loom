import { lazy, Suspense } from "react";
import { isAuthDisabled } from "../api";
import { pluginInstallLines } from "../lib/clients";
import { DOCS_MCP_URL } from "../flows/TokenGate";
import { Modal } from "./ui/Modal";
import { ExternalLink, Loading } from "./ui";
import { CopyButton } from "./CopyButton";

/** The token form, with the dialog that shows each client's setup once a token is made:
 *  the Tokens page's own, loaded only when the dialog opens. */
const QuickConnect = lazy(() => import("../views/Tokens").then((m) => ({ default: m.QuickConnect })));

const INSTRUCTIONS_ID = "connect-agent-instructions";

/**
 * ConnectAgentDialog — connecting an agent from wherever the page is: an empty list that
 * only an agent fills opens it in place, with no move to Settings. With auth on it is the
 * token form (the Tokens page's `QuickConnect`); with auth off, the plugin install lines,
 * since that server takes no token. Opening focuses the instructions, so a reader starts
 * on what the dialog is for; closing returns focus to the opener (`ui/Modal`). Pages open it
 * through `ConnectAgentButton`, which loads this module on the first press.
 */
export function ConnectAgentDialog({
  open,
  onOpenChange,
  onCloseAutoFocus,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCloseAutoFocus?: (e: Event) => void;
}) {
  const authOff = isAuthDisabled();
  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title="Connect an agent"
      className="w-full max-w-[720px]"
      onOpenAutoFocus={(e) => {
        e.preventDefault();
        document.getElementById(INSTRUCTIONS_ID)?.focus();
      }}
      onCloseAutoFocus={onCloseAutoFocus}
    >
      {authOff ? (
        <AuthOffConnect instructionsId={INSTRUCTIONS_ID} />
      ) : (
        <>
          <p id={INSTRUCTIONS_ID} tabIndex={-1} className="text-sm text-text-muted">
            Your agent saves what it learns here and reads it back in its next session. Make a token for it below; the
            next step shows how to add it to your agent.
          </p>
          <div className="mt-1 mb-3">
            <ExternalLink href={DOCS_MCP_URL} size="sm">
              Learn more: connecting an agent
            </ExternalLink>
          </div>
          <Suspense fallback={<Loading layout="inline" label="Loading the token form" />}>
            {/* Cancel, or the token's setup dialog closing, is done with: the dialog closes. */}
            <QuickConnect focusTitle={false} onMinted={() => {}} onClose={() => onOpenChange(false)} />
          </Suspense>
        </>
      )}
    </Modal>
  );
}

/** A single-user server takes no token: the plugin needs only the address. */
export function AuthOffConnect({ instructionsId }: { instructionsId?: string }) {
  const lines = `${pluginInstallLines("claude-code")} --config server_url=${window.location.origin}`;
  return (
    <div className="mt-2">
      <p id={instructionsId} tabIndex={instructionsId ? -1 : undefined} className="text-xs text-text-muted mb-1">
        This server runs for one person, so no token is needed. For Claude Code, run this once in a terminal, then start
        a new session; other agents connect from Settings › Tokens.
      </p>
      <CopyBlock text={lines} label="Copy commands" />
    </div>
  );
}

function CopyBlock({ text, label }: { text: string; label: string }) {
  return (
    <div className="relative">
      <pre className="text-xs font-mono bg-bg border border-border rounded-sm p-2.5 pr-32 max-h-40 overflow-auto whitespace-pre-wrap break-all">
        {text}
      </pre>
      <CopyButton text={text} label={label} className="absolute top-1.5 right-1.5" />
    </div>
  );
}
