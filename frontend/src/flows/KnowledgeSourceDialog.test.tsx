/**
 * Adding a document source checks first — `knowledge.source.add(dry_run)`, nothing
 * stored — and Add sends exactly the form that passed, each kind with its own config.
 * A key is typed into a password field, and no toast, error or text on the page ever
 * carries it. Check probes on open and stops when the dialog closes; Sync now records a
 * request; Retire needs the typed name and keeps the documents unless asked.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  addKnowledgeSource,
  checkKnowledgeSource,
  removeKnowledgeSource,
  rotateKnowledgeSource,
  syncKnowledgeSourceNow,
} from "../api/knowledgeSources";
import { toast } from "../components/Toast";
import { ApiError } from "../lib/errors";
import { sourceAddBody, type SourceForm } from "../lib/knowledgeSources";
import { axeViolations, unlabeledControls } from "../test/axe";
import {
  AddSourceDialog,
  CheckSourceDialog,
  RetireSourceDialog,
  RotateSourceDialog,
  SyncNowDialog,
} from "./KnowledgeSourceDialog";

vi.mock("../api/knowledgeSources", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/knowledgeSources")>();
  return {
    ...actual,
    addKnowledgeSource: vi.fn(),
    checkKnowledgeSource: vi.fn(),
    removeKnowledgeSource: vi.fn(),
    rotateKnowledgeSource: vi.fn(),
    syncKnowledgeSourceNow: vi.fn(),
  };
});
vi.mock("../components/Toast", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../components/Toast")>();
  return { ...actual, toast: vi.fn() };
});

const SECRET = "secret-value-7f3a";
const KEY = `{"type": "service_account", "private_key": "${SECRET}"}`;
const FOLDER = "1AbCdEfGhIjKlMnOp";
const NS = { org: "o", namespace: "code-x" };

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const refused = () =>
  new ApiError({ status: 400, code: "CREDENTIAL_REFUSED", message: "the source refused the credential: http_401" });

/** Nothing the page shows — its text, a field's value outside the password input, a
 *  toast — carries the key. */
function expectKeyNowhere() {
  expect(document.body.textContent ?? "").not.toContain(SECRET);
  for (const el of document.querySelectorAll("input, textarea")) {
    if ((el as HTMLInputElement).type !== "password") expect((el as HTMLInputElement).value).not.toContain(SECRET);
  }
  for (const call of vi.mocked(toast).mock.calls) expect(String(call[0])).not.toContain(SECRET);
}

async function fill() {
  const user = userEvent.setup();
  fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: "design-docs" } });
  await user.click(screen.getByLabelText(/^Folders/));
  await user.paste(FOLDER);
  fireEvent.change(screen.getByLabelText(/^Service account key/), { target: { value: KEY } });
}

const addResult = (dryRun: boolean) => ({
  name: "design-docs",
  kind: "gdrive",
  namespace: "code-x",
  config: {},
  probe: { documents: 12, more: false, sample: ["Storage design"] },
  dry_run: dryRun,
});

function renderAdd(onAdded = vi.fn()) {
  render(<AddSourceDialog open onOpenChange={() => {}} {...NS} repo="Acme/x" onAdded={onAdded} />);
  return onAdded;
}

describe("AddSourceDialog", () => {
  it("checks before it adds, and adds only the form that passed", async () => {
    vi.mocked(addKnowledgeSource).mockImplementation(async (_id, _f, dryRun) => addResult(dryRun));
    const onAdded = renderAdd();
    const add = screen.getByRole("button", { name: "Add source" });
    expect(add).toBeDisabled();
    expect(screen.getByRole("button", { name: "Check" })).toBeDisabled();
    await fill();
    expect(screen.getByLabelText(/^Service account key/)).toHaveAttribute("type", "password");
    fireEvent.click(screen.getByRole("button", { name: "Check" }));
    expect(await screen.findByText(/lists 12 documents/)).toBeInTheDocument();
    expect(screen.getByText("Storage design")).toBeInTheDocument();
    expect(vi.mocked(addKnowledgeSource).mock.calls[0]?.[2]).toBe(true);
    expect(add).toBeEnabled();

    // A change after the check turns Add off again; undoing it turns Add back on.
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: "design-notes" } });
    expect(add).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: "design-docs" } });
    expect(add).toBeEnabled();
    fireEvent.click(add);
    await waitFor(() => expect(onAdded).toHaveBeenCalledWith("design-docs"));
    expect(vi.mocked(addKnowledgeSource).mock.calls[1]?.[2]).toBe(false);
    expectKeyNowhere();
    expect(unlabeledControls(document.body)).toEqual([]);
    expect(await axeViolations(document.body)).toEqual([]);
  });

  it("unticking and ticking a file kind again leaves the checked form, so Add stays on", async () => {
    vi.mocked(addKnowledgeSource).mockImplementation(async (_id, _f, dryRun) => addResult(dryRun));
    renderAdd();
    await fill();
    fireEvent.click(screen.getByRole("button", { name: "Check" }));
    await screen.findByText(/lists 12 documents/);
    const add = screen.getByRole("button", { name: "Add source" });
    const docs = screen.getByRole("checkbox", { name: "Documents" });
    fireEvent.click(docs);
    expect(add).toBeDisabled();
    fireEvent.click(docs);
    expect(add).toBeEnabled();
  });

  it("shows a refused check, keeps Add off, and the key appears nowhere", async () => {
    vi.mocked(addKnowledgeSource).mockRejectedValue(refused());
    renderAdd();
    await fill();
    fireEvent.click(screen.getByRole("button", { name: "Check" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("refused the credential");
    expect(screen.getByRole("button", { name: "Add source" })).toBeDisabled();
    expectKeyNowhere();
  });

  it("a refused add keeps the passed check, so Add can be tried again", async () => {
    vi.mocked(addKnowledgeSource).mockImplementation(async (_id, _f, dryRun) => {
      if (dryRun) return addResult(true);
      throw new ApiError({ status: 503, code: "OVERLOADED", message: "the namespace's sources are busy" });
    });
    renderAdd();
    await fill();
    fireEvent.click(screen.getByRole("button", { name: "Check" }));
    await screen.findByText(/lists 12 documents/);
    const add = screen.getByRole("button", { name: "Add source" });
    fireEvent.click(add);
    expect(await screen.findByRole("alert")).toHaveTextContent("busy");
    expect(add).toBeEnabled();
    expect(screen.getByText(/lists 12 documents/)).toBeInTheDocument();
    expectKeyNowhere();
  });
});

/** The request body the dialog's `n`th add call sends. */
function sentBody(n: number) {
  const call = vi.mocked(addKnowledgeSource).mock.calls[n];
  if (!call) throw new Error(`no add call ${n}`);
  return sourceAddBody(call[0].org, call[0].namespace, call[1] as SourceForm, call[2]);
}

const PROJECT = "0f8b2c4e-1a2b-4c3d-8e9f-0123456789ab";
const PAGE = "1a2b3c4d5e6f708192a3b4c5d6e7f809";
const DATABASE = "abcdefabcdefabcdefabcdefabcdef01";

describe("AddSourceDialog's kinds", () => {
  async function typeInto(name: RegExp, text: string) {
    const user = userEvent.setup();
    await user.click(screen.getByRole("textbox", { name }));
    await user.paste(text);
  }

  async function checkAndAdd(lead: RegExp) {
    vi.mocked(addKnowledgeSource).mockImplementation(async (_id, _f, dryRun) => addResult(dryRun));
    fireEvent.click(screen.getByRole("button", { name: "Check" }));
    expect(await screen.findByText(lead)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add source" }));
    await waitFor(() => expect(addKnowledgeSource).toHaveBeenCalledTimes(2));
    expect(sentBody(1)).toEqual({ ...sentBody(0), dry_run: false });
  }

  it("offers each kind, the shared folder first", () => {
    renderAdd();
    const kinds = screen.getAllByRole("radio").map((r) => r.getAttribute("aria-label"));
    expect(kinds).toEqual(["Shared folder", "Project documents", "Pages", "Meeting transcripts"]);
    expect(screen.getByRole("radio", { name: "Shared folder" })).toHaveAttribute("aria-checked", "true");
  });

  it("adds a project tracker's projects with a read-only API key", async () => {
    renderAdd();
    fireEvent.click(screen.getByRole("radio", { name: "Project documents" }));
    expect(screen.queryByLabelText(/^Folders/)).toBeNull();
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: "roadmap" } });
    await typeInto(/^Projects/, PROJECT);
    const key = screen.getByLabelText(/^Read-only API key/);
    expect(key).toHaveAttribute("type", "password");
    fireEvent.change(key, { target: { value: SECRET } });
    await checkAndAdd(/The key reads the projects: roadmap lists 12 documents/);
    expect(sentBody(0)).toEqual({
      org: "o",
      namespace: "code-x",
      name: "roadmap",
      kind: "linear",
      config: { project_ids: [PROJECT] },
      credential: SECRET,
      dry_run: true,
    });
    expectKeyNowhere();
    expect(unlabeledControls(document.body)).toEqual([]);
    expect(await axeViolations(document.body)).toEqual([]);
  });

  it("adds a page store's pages and databases with an integration token", async () => {
    renderAdd();
    fireEvent.click(screen.getByRole("radio", { name: "Pages" }));
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: "wiki" } });
    await typeInto(/^Pages/, `https://pages.example/team/Home-${PAGE}`);
    await typeInto(/^Databases/, DATABASE);
    const token = screen.getByLabelText(/^Integration token/);
    expect(token).toHaveAttribute("type", "password");
    fireEvent.change(token, { target: { value: SECRET } });
    await checkAndAdd(/The token reads the pages and databases: wiki lists 12 documents/);
    expect(sentBody(0)).toMatchObject({
      kind: "notion",
      config: { page_ids: [PAGE], database_ids: [DATABASE] },
      credential: SECRET,
    });
    expectKeyNowhere();
    expect(unlabeledControls(document.body)).toEqual([]);
    expect(await axeViolations(document.body)).toEqual([]);
  });

  it("adds a meeting store's meetings, each provider with its own ids", async () => {
    renderAdd();
    fireEvent.click(screen.getByRole("radio", { name: "Meeting transcripts" }));
    expect(screen.getByRole("radio", { name: "meet" })).toHaveAttribute("aria-checked", "true");
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: "standups" } });
    await typeInto(/^Meeting spaces/, "spaces/abc-DEF_1");
    fireEvent.click(screen.getByRole("radio", { name: "zoom" }));
    // A space id is no meeting id: the form says so before anything is sent.
    expect(screen.getByLabelText(/^Meetings/)).toHaveValue("spaces/abc-DEF_1");
    const credential = screen.getByLabelText(/^Read-only credential/);
    expect(credential).toHaveAttribute("type", "password");
    fireEvent.change(credential, { target: { value: KEY } });
    expect(screen.getByText(/Not a meeting id \(9-12 digits\): spaces\/abc-DEF_1/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Check" })).toBeDisabled();
    await userEvent.setup().clear(screen.getByRole("textbox", { name: /^Meetings/ }));
    await typeInto(/^Meetings/, "81234567890");
    await checkAndAdd(/The credential reads the meetings: standups lists 12 documents/);
    expect(sentBody(0)).toMatchObject({
      kind: "recording",
      config: { provider: "zoom", ids: ["81234567890"] },
      credential: KEY,
    });
    expectKeyNowhere();
    expect(unlabeledControls(document.body)).toEqual([]);
    expect(await axeViolations(document.body)).toEqual([]);
  });

  it("a check of one kind does not pass the form once the kind changes", async () => {
    vi.mocked(addKnowledgeSource).mockImplementation(async (_id, _f, dryRun) => addResult(dryRun));
    renderAdd();
    await fill();
    fireEvent.click(screen.getByRole("button", { name: "Check" }));
    await screen.findByText(/lists 12 documents/);
    const add = screen.getByRole("button", { name: "Add source" });
    expect(add).toBeEnabled();
    fireEvent.click(screen.getByRole("radio", { name: "Project documents" }));
    expect(add).toBeDisabled();
    // The key was typed for the shared folder: another kind never sends it.
    expect(screen.getByLabelText(/^Read-only API key/)).toHaveValue("");
    // Back to the shared folder: the folders typed are still there; the key is asked
    // for again, and the same key gives the form that passed.
    fireEvent.click(screen.getByRole("radio", { name: "Shared folder" }));
    expect(screen.getByLabelText(/^Folders/)).toHaveValue(FOLDER);
    expect(screen.getByLabelText(/^Service account key/)).toHaveValue("");
    fireEvent.change(screen.getByLabelText(/^Service account key/), { target: { value: KEY } });
    expect(add).toBeEnabled();
  });

  it("clears the credential when the provider changes, so one provider's credential never goes to the other", async () => {
    renderAdd();
    fireEvent.click(screen.getByRole("radio", { name: "Meeting transcripts" }));
    fireEvent.change(screen.getByLabelText(/^Read-only credential/), { target: { value: KEY } });
    fireEvent.click(screen.getByRole("radio", { name: "zoom" }));
    expect(screen.getByLabelText(/^Read-only credential/)).toHaveValue("");
  });

  it("locks the kind and the provider while a check runs", async () => {
    let answer: (v: ReturnType<typeof addResult>) => void = () => {};
    vi.mocked(addKnowledgeSource).mockImplementation(() => new Promise((resolve) => (answer = resolve)));
    renderAdd();
    fireEvent.click(screen.getByRole("radio", { name: "Meeting transcripts" }));
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: "standups" } });
    await typeInto(/^Meeting spaces/, "abc");
    fireEvent.change(screen.getByLabelText(/^Read-only credential/), { target: { value: KEY } });
    fireEvent.click(screen.getByRole("button", { name: "Check" }));
    expect(screen.getByRole("radio", { name: "Pages" })).toBeDisabled();
    expect(screen.getByRole("radio", { name: "zoom" })).toBeDisabled();
    await act(async () => answer(addResult(true)));
    expect(screen.getByRole("radio", { name: "Pages" })).toBeEnabled();
    expect(screen.getByRole("radio", { name: "zoom" })).toBeEnabled();
  });

  it("shows the server's refusal of a config as it comes back", async () => {
    const message = "config.project_ids: at most 20 projects";
    vi.mocked(addKnowledgeSource).mockRejectedValue(new ApiError({ status: 400, code: "BAD_CONFIG", message }));
    renderAdd();
    fireEvent.click(screen.getByRole("radio", { name: "Project documents" }));
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: "roadmap" } });
    await typeInto(/^Projects/, PROJECT);
    fireEvent.change(screen.getByLabelText(/^Read-only API key/), { target: { value: SECRET } });
    fireEvent.click(screen.getByRole("button", { name: "Check" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.getByRole("button", { name: "Add source" })).toBeDisabled();
    expectKeyNowhere();
  });

  it("shows the server's refusal as it comes back", async () => {
    // What the server answers when the account cannot see the space.
    const message = "the source could not be listed: space abc: http_404; check the id and that the account can see it";
    vi.mocked(addKnowledgeSource).mockRejectedValue(new ApiError({ status: 400, code: "SOURCE_UNREACHABLE", message }));
    renderAdd();
    fireEvent.click(screen.getByRole("radio", { name: "Meeting transcripts" }));
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: "standups" } });
    await typeInto(/^Meeting spaces/, "abc");
    fireEvent.change(screen.getByLabelText(/^Read-only credential/), { target: { value: KEY } });
    fireEvent.click(screen.getByRole("button", { name: "Check" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    // The error was the meeting store's: another kind does not show it.
    fireEvent.click(screen.getByRole("radio", { name: "Pages" }));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("button", { name: "Add source" })).toBeDisabled();
    expectKeyNowhere();
  });
});

describe("RotateSourceDialog", () => {
  it("takes the key in a password field and sends it once", async () => {
    vi.mocked(rotateKnowledgeSource).mockResolvedValue({
      name: "design-docs",
      probe: { documents: 1, more: false, sample: [] },
      rotated_at: "2026-11-01T00:00:00Z",
    });
    const onRotated = vi.fn();
    render(
      <RotateSourceDialog
        open
        onOpenChange={() => {}}
        {...NS}
        name="design-docs"
        kind="gdrive"
        onRotated={onRotated}
      />,
    );
    const rotate = screen.getByRole("button", { name: "Rotate" });
    expect(rotate).toBeDisabled();
    const field = screen.getByLabelText(/^Service account key/);
    expect(field).toHaveAttribute("type", "password");
    fireEvent.change(field, { target: { value: KEY } });
    fireEvent.click(rotate);
    await waitFor(() => expect(onRotated).toHaveBeenCalled());
    expect(rotateKnowledgeSource).toHaveBeenCalledTimes(1);
    expect(rotateKnowledgeSource).toHaveBeenCalledWith(NS, "design-docs", KEY);
    expectKeyNowhere();
  });

  it("shows a refused key without echoing it", async () => {
    vi.mocked(rotateKnowledgeSource).mockRejectedValue(refused());
    render(
      <RotateSourceDialog open onOpenChange={() => {}} {...NS} name="design-docs" kind="gdrive" onRotated={() => {}} />,
    );
    fireEvent.change(screen.getByLabelText(/^Service account key/), { target: { value: KEY } });
    fireEvent.click(screen.getByRole("button", { name: "Rotate" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("refused the credential");
    expectKeyNowhere();
  });
});

describe("CheckSourceDialog", () => {
  it("probes on open, shows what it found, and probes again on Check again", async () => {
    vi.mocked(checkKnowledgeSource).mockResolvedValue({
      name: "design-docs",
      reachable: true,
      documents: 40,
      more: false,
      sample: ["Storage design"],
      summary: "",
      checked_at: "2026-11-01T00:00:00Z",
    });
    render(<CheckSourceDialog open onOpenChange={() => {}} {...NS} name="design-docs" />);
    expect(await screen.findByText(/Reachable: it lists 40 documents/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    await waitFor(() => expect(checkKnowledgeSource).toHaveBeenCalledTimes(2));
  });

  it("says why an unreachable source failed", async () => {
    vi.mocked(checkKnowledgeSource).mockResolvedValue({
      name: "design-docs",
      reachable: false,
      documents: 0,
      more: false,
      sample: [],
      summary: "a folder is not shared with the account",
      checked_at: "2026-11-01T00:00:00Z",
    });
    render(<CheckSourceDialog open onOpenChange={() => {}} {...NS} name="design-docs" />);
    expect(await screen.findByText("a folder is not shared with the account")).toBeInTheDocument();
  });

  it("aborts the probe when the dialog closes", async () => {
    let signal: AbortSignal | undefined;
    vi.mocked(checkKnowledgeSource).mockImplementation((_id, _name, opts) => {
      signal = opts?.signal;
      return new Promise(() => {});
    });
    const { unmount } = render(<CheckSourceDialog open onOpenChange={() => {}} {...NS} name="design-docs" />);
    await waitFor(() => expect(signal).toBeDefined());
    expect(signal?.aborted).toBe(false);
    act(() => unmount());
    expect(signal?.aborted).toBe(true);
  });
});

describe("SyncNowDialog", () => {
  it("records a request, full when asked, and says when sync is off", async () => {
    vi.mocked(syncKnowledgeSourceNow).mockResolvedValue({ name: "design-docs", enabled: false });
    const onRequested = vi.fn();
    render(<SyncNowDialog open onOpenChange={() => {}} {...NS} name="design-docs" onRequested={onRequested} />);
    fireEvent.click(screen.getByRole("checkbox", { name: /List every document/ }));
    fireEvent.click(screen.getByRole("button", { name: "Request sync" }));
    await waitFor(() => expect(onRequested).toHaveBeenCalled());
    expect(syncKnowledgeSourceNow).toHaveBeenCalledWith(NS, "design-docs", true);
    expect(String(vi.mocked(toast).mock.calls[0]?.[0])).toContain("sync is off");
  });
});

describe("RetireSourceDialog", () => {
  function renderRetire(enabled: boolean, onRetired = vi.fn()) {
    render(
      <RetireSourceDialog
        open
        onOpenChange={() => {}}
        {...NS}
        name="design-docs"
        enabled={enabled}
        onRetired={onRetired}
      />,
    );
    return onRetired;
  }

  it("needs the typed name and keeps the documents unless asked", async () => {
    vi.mocked(removeKnowledgeSource).mockResolvedValue({ name: "design-docs", documents_pending: 0 });
    const onRetired = renderRetire(true);
    const retire = screen.getByRole("button", { name: "Retire source" });
    expect(retire).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/to confirm/), { target: { value: "design-docs" } });
    fireEvent.click(retire);
    await waitFor(() => expect(onRetired).toHaveBeenCalled());
    expect(removeKnowledgeSource).toHaveBeenCalledWith(NS, "design-docs", "design-docs", false);
    expect(vi.mocked(toast).mock.calls[0]?.[0]).toBe("Retired design-docs; its documents stay");
  });

  it("with sync off, says the documents wait until sync is on", async () => {
    vi.mocked(removeKnowledgeSource).mockResolvedValue({ name: "design-docs", documents_pending: 1 });
    const onRetired = renderRetire(false);
    fireEvent.click(screen.getByRole("checkbox", { name: /Also retire the documents/ }));
    fireEvent.change(screen.getByLabelText(/to confirm/), { target: { value: "design-docs" } });
    fireEvent.click(screen.getByRole("button", { name: "Retire source" }));
    await waitFor(() => expect(onRetired).toHaveBeenCalled());
    expect(removeKnowledgeSource).toHaveBeenCalledWith(NS, "design-docs", "design-docs", true);
    expect(vi.mocked(toast).mock.calls[0]?.[0]).toBe(
      "Retired design-docs; its 1 document is retired once sync is on for this deployment",
    );
  });
});
