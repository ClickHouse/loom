import { useEffect, useState } from "react";
import { Alert } from "../components/ui/Alert";
import { Button } from "@clickhouse/click-ui/Button";
import { listProfile, type ProfileEntry } from "../api";
import { useStore } from "../lib/state";
import { Skeleton } from "../components/Skeleton";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { Unavailable } from "../components/ui/Unavailable";
import { useOpReason } from "../lib/useOpReason";
import { LoadingSkeleton } from "../components/ui/Loading";
import { relativeTime } from "../lib/format";

// Read-only preview of a Profile section; editing lives on /profile
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
  // Editing is profile.set (WRITER): below that, Edit stays in view with what it needs.
  const editReason = useOpReason("profile.set", identity?.namespace ?? "");
  const close = () => inspectProfile(null);
  const editOnProfile = () => {
    if (!label || editReason) return;
    requestEditProfile(label);
  };

  return (
    <Modal
      open={open}
      onOpenChange={(v) => {
        if (!v) close();
      }}
      // A read-only view: it opens on itself, not on Edit, whose tooltip would come up with
      // it and take the first Escape.
      initialFocus="dialog"
      title={label ?? ""}
      className="w-full max-w-[720px]"
    >
      {section && (
        <div className="mb-4 flex flex-wrap items-center gap-2 text-xs text-text-muted tabular-nums">
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
      <div>
        {loading && (
          <LoadingSkeleton label="Loading the profile section">
            <div className="space-y-3">
              <Skeleton className="h-4 w-3/4" />
              <Skeleton className="h-4 w-full" />
              <Skeleton className="h-4 w-2/3" />
            </div>
          </LoadingSkeleton>
        )}
        {error && <Alert role="alert" state="danger" text={error} />}
        {!loading && !error && section && (
          <pre className="whitespace-pre-wrap break-words font-sans text-sm text-text leading-relaxed">
            {section.value}
          </pre>
        )}
      </div>
      <ModalFooter>
        <Unavailable reason={editReason} tip="Edit this section on the Profile page">
          <Button type="secondary" htmlType="button" iconLeft="pencil" onClick={editOnProfile}>
            Edit
          </Button>
        </Unavailable>
      </ModalFooter>
    </Modal>
  );
}
