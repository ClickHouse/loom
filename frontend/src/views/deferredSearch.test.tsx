import { cleanup, render, screen } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { Documents } from "./Documents";
import { Profile } from "./Profile";
import { Audit } from "./admin/Audit";

// A page that filters its rows by the deferred search text says its count and its
// no-match state of that same text: in the render where the box has moved on and the
// rows have not, the words still describe the rows on screen. `held` stops every
// deferred value where it is, as React does for the render before the deferred one.

const held = vi.hoisted(() => ({ on: false }));
vi.mock("react", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react")>();
  return {
    ...actual,
    useDeferredValue<T>(value: T): T {
      const last = actual.useRef(value);
      actual.useDeferredValue(value);
      if (!held.on) last.current = value;
      return last.current;
    },
  };
});

const api = vi.hoisted(() => ({
  listKnowledge: vi.fn(),
  listProfile: vi.fn(),
  getProfileActivity: vi.fn(async () => ({ entries: [] })),
  listAuditLog: vi.fn(),
  listErasures: vi.fn(async () => ({ rows: [] })),
}));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  ...api,
  isAuthDisabled: () => false,
}));

const doc = (source_uri: string, title: string) => ({
  source_uri,
  title,
  description: "",
  tags: [],
  ingested_at: "2026-09-28T12:00:00Z",
  chunk_count: 3,
  superseded: false,
  has_blob: false,
  blob_size: 0,
  blob_content_type: "",
  original_filename: "",
});
const section = (label: string, value: string) => ({
  label,
  value,
  char_limit: 8192,
  content_sha256: "x",
  version: 1,
  created_at: "2026-09-20T00:00:00Z",
  updated_at: "2026-09-27T12:00:00Z",
  last_edited_by: "loom-ui",
});
const event = (actor: string) => ({
  ts: new Date().toISOString(),
  event_type: "namespace.register",
  actor,
  subject: "{}",
  request_id: "",
});

const toolbar = () => screen.getByLabelText("Search and filters");

async function typeHeld(box: HTMLElement, text: string) {
  held.on = true;
  await userEvent.type(box, text);
}
async function clearHeld(box: HTMLElement) {
  held.on = true;
  await userEvent.clear(box);
}

beforeEach(() => {
  held.on = false;
  api.listKnowledge.mockResolvedValue({
    rows: [doc("docs/a.md", "Refund escalation"), doc("docs/b.md", "Ingest spec")],
    next_cursor: null,
  });
  api.listProfile.mockResolvedValue({ entries: [section("about-me", "On call."), section("persona", "Terse.")] });
  api.listAuditLog.mockResolvedValue({ rows: [event("alice"), event("bob")] });
  useStore.setState({ authed: true, identity: { org: "acme", namespace: "team", agent: "loom-ui" } });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  useStore.setState({ identity: null });
});

describe("the count and the no-match state follow the rows, not the box", () => {
  it("Library: a word the rows have not taken yet leaves the count at what is shown", async () => {
    render(<Documents />);
    await screen.findByText("Refund escalation");
    await typeHeld(screen.getByLabelText("Search library"), "refund");
    expect(screen.getByText("Ingest spec")).toBeInTheDocument();
    expect(toolbar()).toHaveTextContent("2 shown");
  });

  it("Library: clearing a search that matched nothing keeps saying so until the rows return", async () => {
    render(<Documents />);
    await screen.findByText("Refund escalation");
    const box = screen.getByLabelText("Search library");
    await userEvent.type(box, "zzz");
    await screen.findByText("zzz", { selector: ".font-mono" });
    await clearHeld(box);
    expect(toolbar()).toHaveTextContent("0 matches");
    expect(screen.getByText("zzz", { selector: ".font-mono" })).toBeInTheDocument();
  });

  it("Profile: the range stays while the sections have not filtered, and no match is said of the rows' text", async () => {
    render(<Profile />);
    await screen.findByText("about-me");
    const box = screen.getByLabelText("Search profile sections");
    await typeHeld(box, "zzz");
    expect(toolbar()).toHaveTextContent("1–2 of 2");
    held.on = false;
    await userEvent.type(box, "z");
    await screen.findByText("zzzz", { selector: ".font-mono" });
    await clearHeld(box);
    expect(toolbar()).toHaveTextContent("0 matches");
    expect(screen.getByText("zzzz", { selector: ".font-mono" })).toBeInTheDocument();
  });

  it("Activity: clearing a filter that matched nothing names it until the rows return", async () => {
    render(<Audit />);
    await screen.findByText("alice");
    const box = screen.getByRole("textbox", { name: "Filter audit events" });
    await userEvent.type(box, "zzz");
    await screen.findByText("zzz", { selector: ".font-mono" });
    await clearHeld(box);
    expect(screen.getByText("zzz", { selector: ".font-mono" })).toBeInTheDocument();
    expect(screen.queryByText("the filters in force", { exact: false })).toBeNull();
  });
});
