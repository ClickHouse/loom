import { render, screen } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { axeViolations, unlabeledControls } from "../test/axe";
import { useStore } from "../lib/state";
import { AddMemoryDialog } from "./AddMemoryDialog";
import { AddDocumentDialog } from "./AddDocumentDialog";
import { CopyToNamespaceDialog } from "./CopyToNamespaceDialog";
import { CreateNamespaceDialog } from "./CreateNamespaceDialog";
import { EditDocumentMetadataDialog } from "./EditDocumentMetadataDialog";
import { MembersDialog } from "./MembersDialog";
import { NamespaceSettingsDialog } from "./NamespaceSettingsDialog";
import { TaskEditDialog } from "./TaskEditDialog";
import { TaskActionDialog } from "./TaskActionDialog";
import { InboxSettingsDialog } from "./InboxSettingsDialog";

// Every dialog renders open, and every input, select and textarea in it has a name —
// through `<label htmlFor>`, a wrapping `<label>` or `aria-label`. axe runs over the
// whole document because Radix portals the dialog to `document.body`. An unlabelled
// control is announced by a screen reader as "edit text".

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listMemories: vi.fn(async () => ({ entries: [], truncated: false, total: 0 })),
    listMine: vi.fn(async () => [
      {
        namespace: "team",
        role: "writer",
        status: "active",
        visibility: "private",
        description: "",
        joined_at: null,
        kind: "memory",
      },
      {
        namespace: "other",
        role: "writer",
        status: "active",
        visibility: "private",
        description: "",
        joined_at: null,
        kind: "memory",
      },
    ]),
    listGrants: vi.fn(async () => ({
      rows: [
        {
          subject: "peer@example.com",
          role: "viewer",
          granted_at: "2026-01-01T00:00:00Z",
          granted_by: "op@example.com",
        },
      ],
    })),
    getNamespace: vi.fn(async () => ({
      namespace: "team",
      owner_email: "op@example.com",
      description: "shared",
      tags: [],
      active: true,
      consolidation_enabled: false,
      conflict_threshold: null,
      visibility: "private",
      requires_approval: false,
      join_policy: "inherit",
      effective_join_policy: "approval",
      kind: "memory",
      created_at: "2026-01-01T00:00:00Z",
    })),
  };
});

const identity = { org: "dev", namespace: "team", agent: "dashboard" };

// The task inbox plane's dialogs.
vi.mock("../api/tasks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/tasks")>();
  return {
    ...actual,
    listInboxes: vi.fn(async () => []),
    // The poster picker's directory: one person listed, one bot that posts anyway.
    listPrincipals: vi.fn(async () => ({
      principals: [
        {
          subject: "alice@example.com",
          label: "alice@example.com",
          kind: "human",
          namespaces: ["team", "other", "qa", "code-x"],
          roles: ["writer"],
          last_seen: "2026-09-19T22:00:00Z",
          can_already_post: false,
          listed: true,
        },
        {
          subject: "sa:team-bot-9f8e7d",
          label: "team-bot",
          kind: "service_account",
          namespaces: ["team"],
          roles: ["writer"],
          last_seen: null,
          can_already_post: true,
          listed: false,
        },
      ],
      truncated: false,
    })),
    listPolicies: vi.fn(async () => [
      {
        org: "dev",
        inbox: "private-bot",
        poster: "sa:reporter-public-1a2b3c",
        kind: "bug_report",
        mode: "review",
        require_evidence: ["repro"],
        set_by: "human:op@example.com",
        set_at: "2026-09-20T00:00:00",
        version: 1,
      },
    ]),
  };
});
const INBOX = {
  org: "dev",
  inbox: "private-bot",
  owner_agent: "reporter-private",
  owner_namespace: "team",
  description: "",
  posters: [],
  accept_disclosure: ["internal" as const],
  default_lease_seconds: 1800,
  max_attempts: 3,
  max_defers: 5,
  triage_escalate_after_seconds: 14400,
  webhook_url: "",
  created_by: "",
  created_at: "2026-09-20T00:00:00",
  updated_at: "2026-09-20T00:00:00",
  version: 1,
};
const TASK = {
  task_id: "00000000-0000-4000-8000-000000000001",
  org: "dev",
  inbox: "private-bot",
  namespace: "team",
  kind: "bug_report",
  priority: 2,
  disclosure: "internal" as const,
  status: "todo" as const,
  reason: "",
  blocked_on: "",
  not_before: null,
  resume_at: null,
  deadline: "2026-09-30T12:00:00",
  defer_count: 0,
  title: "A regression",
  brief: { objective: "fix it", acceptance_criteria: ["a test"] },
  from_agent: "reporter-public",
  from_subject: "sa:reporter-public-1a2b3c",
  origin: "",
  parent_task_id: null,
  owner: "",
  attempt: 0,
  fence: 0,
  lease_until: null,
  outcome: {},
  last_actor: "",
  created_at: "2026-09-20T00:00:00",
  updated_at: "2026-09-20T00:00:00",
  version: 3,
};
const noop = () => undefined;

async function expectLabelledAndClean(name: string) {
  const dialog = await screen.findByRole("dialog");
  expect(dialog, `${name} did not open`).toBeInTheDocument();
  expect(unlabeledControls(document.body), `${name}: controls without a name`).toEqual([]);
  expect(await axeViolations(document.body), `${name}: axe`).toEqual([]);
}

beforeEach(() => {
  useStore.setState({
    whoami: { authenticated: true, subject: "op@example.com", org: "dev", roles: ["ADMIN"] },
    identity,
  });
});

describe("dialogs: every control is labelled and axe is clean", () => {
  it("AddMemoryDialog", async () => {
    render(<AddMemoryDialog open onOpenChange={noop} identity={identity} onAdded={noop} />);
    await expectLabelledAndClean("AddMemoryDialog");
  });

  it("AddDocumentDialog", async () => {
    render(<AddDocumentDialog open onOpenChange={noop} identity={identity} onAdded={noop} />);
    await expectLabelledAndClean("AddDocumentDialog");
  });

  it("CopyToNamespaceDialog", async () => {
    render(<CopyToNamespaceDialog open onOpenChange={noop} source={identity} memoryKey="user_coffee" />);
    // The destination Select leaves its loading state once the writable namespaces arrive.
    await screen.findByText("Select a namespace…");
    await expectLabelledAndClean("CopyToNamespaceDialog");
  });

  it("CreateNamespaceDialog", async () => {
    render(<CreateNamespaceDialog open onOpenChange={noop} onCreated={noop} />);
    await expectLabelledAndClean("CreateNamespaceDialog");
  });

  it("EditDocumentMetadataDialog", async () => {
    const entry = {
      source_uri: "docs/a.md",
      title: "A",
      tags: ["x"],
      ingested_at: "2026-01-01T00:00:00Z",
      chunk_count: 1,
      superseded: false,
    };
    render(<EditDocumentMetadataDialog open onOpenChange={noop} identity={identity} entry={entry} onSaved={noop} />);
    await expectLabelledAndClean("EditDocumentMetadataDialog");
  });

  it("MembersDialog, with a member row and the add-member form open", async () => {
    render(<MembersDialog open onOpenChange={noop} org="dev" namespace="team" kind="memory" />);
    await screen.findByText("peer@example.com");
    // The dialog's own "Add member" button reveals the subject + role form.
    await userEvent.setup().click(screen.getAllByRole("button", { name: /Add member/ })[0]);
    await screen.findByRole("textbox", { name: /member to add/ });
    await expectLabelledAndClean("MembersDialog");
  });

  it("NamespaceSettingsDialog", async () => {
    render(<NamespaceSettingsDialog open onOpenChange={noop} org="dev" namespace="team" />);
    await screen.findByDisplayValue("shared");
    await expectLabelledAndClean("NamespaceSettingsDialog");
  });

  it("TaskEditDialog, creating", async () => {
    render(
      <TaskEditDialog
        open
        onOpenChange={noop}
        identity={identity}
        mode={{ kind: "create", inbox: "private-bot", inboxes: [INBOX, { ...INBOX, inbox: "pool" }] }}
        onSaved={noop}
      />,
    );
    await expectLabelledAndClean("TaskEditDialog (create)");
  });

  it("TaskEditDialog, editing", async () => {
    render(
      <TaskEditDialog
        open
        onOpenChange={noop}
        identity={identity}
        mode={{ kind: "edit", task: TASK }}
        onSaved={noop}
      />,
    );
    await screen.findByDisplayValue("A regression");
    await expectLabelledAndClean("TaskEditDialog (edit)");
  });

  it("TaskActionDialog, for each transition", async () => {
    for (const action of ["accept", "wont_do", "defer", "block", "unblock", "cancel", "reassign", "reopen"] as const) {
      const { unmount } = render(
        <TaskActionDialog
          open
          onOpenChange={noop}
          identity={identity}
          action={action}
          task={TASK}
          inboxes={[INBOX, { ...INBOX, inbox: "pool" }]}
          onDone={noop}
        />,
      );
      await expectLabelledAndClean(`TaskActionDialog (${action})`);
      unmount();
    }
  });

  it("InboxSettingsDialog, editing, with the trust table and the add-rule form open", async () => {
    render(<InboxSettingsDialog open onOpenChange={noop} identity={identity} inbox={INBOX} onSaved={noop} />);
    await screen.findByText("sa:reporter-public-1a2b3c");
    await screen.findByRole("checkbox", { name: /alice@example\.com/ });
    await userEvent.setup().click(screen.getByRole("button", { name: /Add rule/ }));
    await screen.findByRole("textbox", { name: /^Poster \(subject/ });
    await expectLabelledAndClean("InboxSettingsDialog");
  });
});
