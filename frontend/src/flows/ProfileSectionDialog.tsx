import { useEffect, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { Edit3, X as XIcon, User as UserIcon } from "lucide-react";
import { listProfile, type ProfileEntry } from "../api";
import { useStore } from "../lib/state";
import { Skeleton } from "../components/Skeleton";
import { relativeTime } from "../lib/format";

// Read-only preview of a Profile section; editing lives on /me/profile
// to avoid duplicating its editor + char-cap + save logic here.
export function ProfileSectionDialog() {
  const label = useStore((s) => s.inspectedProfileLabel);
  const identity = useStore((s) => s.identity);
  const inspectProfile = useStore((s) => s.inspectProfile);
  const [section, setSection] = useState<ProfileEntry | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const open = !!label;

  useEffect(() => {
    if (!open || !identity || !label) return;
    setLoading(true);
    setSection(null);
    setError(null);
    let live = true;
    listProfile(identity)
      .then((r) => {
        if (!live) return;
        const found = (r.entries || []).find((e) => e.label === label);
        if (!found) setError("Section not found in the current namespace.");
        else setSection(found);
      })
      .catch((e) => {
        if (live) setError(String(e?.message || e));
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [open, identity, label]);

  const requestEditProfile = useStore((s) => s.requestEditProfile);
  const close = () => inspectProfile(null);
  const editOnProfile = () => {
    if (!label) return;
    requestEditProfile(label);
  };

  return (
    <Dialog.Root
      open={open}
      onOpenChange={(v) => {
        if (!v) close();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-overlay animate-fade-in motion-reduce:animate-none!" />
        <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-[min(720px,94vw)] max-h-[85vh] flex flex-col rounded-2xl bg-[var(--color-surface)] border border-[var(--color-border)] shadow-2xl animate-fade-in motion-reduce:animate-none!">
          <div className="flex items-start justify-between gap-4 px-6 py-4 border-b border-[var(--color-border)] shrink-0">
            <div className="min-w-0 flex-1">
              <Dialog.Title className="text-md font-semibold tracking-tight truncate font-mono inline-flex items-center gap-2">
                <UserIcon size={14} className="text-[var(--color-text-muted)] shrink-0" />
                {label}
              </Dialog.Title>
              {section && (
                <div className="text-2xs text-[var(--color-text-muted)] mt-1 flex items-center gap-2 flex-wrap tabular-nums">
                  <span>
                    {section.value.length.toLocaleString()}
                    {section.char_limit ? ` / ${section.char_limit.toLocaleString()}` : ""} chars
                  </span>
                  {section.last_edited_by && (
                    <>
                      <span>·</span>
                      <span>
                        edited by <span className="font-mono">{section.last_edited_by}</span>
                      </span>
                    </>
                  )}
                  {section.updated_at && (
                    <>
                      <span>·</span>
                      <span>{relativeTime(section.updated_at)}</span>
                    </>
                  )}
                </div>
              )}
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <button
                onClick={editOnProfile}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-md border border-[var(--color-border)] hover:bg-[var(--color-surface-2)] text-[var(--color-text)]"
                title="Edit this section on the Profile page"
              >
                <Edit3 size={13} /> Edit
              </button>
              <Dialog.Close
                className="p-1.5 text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
                aria-label="Close"
              >
                <XIcon size={16} />
              </Dialog.Close>
            </div>
          </div>

          <div className="flex-1 overflow-y-auto p-6">
            {loading && (
              <div className="space-y-3">
                <Skeleton className="h-4 w-3/4" />
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-4 w-2/3" />
              </div>
            )}
            {error && <div className="text-sm text-[var(--color-danger)]">{error}</div>}
            {!loading && !error && section && (
              <pre className="text-sm whitespace-pre-wrap break-words font-sans text-[var(--color-text)] leading-relaxed">
                {section.value}
              </pre>
            )}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
