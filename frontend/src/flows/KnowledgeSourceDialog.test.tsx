/**
 * Adding a document source checks first — `knowledge.source.add(dry_run)`, nothing
 * stored — and Add sends exactly the form that passed. The credential is typed into a
 * password field and never shown back. Retire needs the typed name and keeps the
 * documents unless asked.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { addKnowledgeSource, removeKnowledgeSource } from "../api/knowledgeSources";
import { ApiError } from "../lib/errors";
import { axeViolations, unlabeledControls } from "../test/axe";
import { AddSourceDialog, RetireSourceDialog } from "./KnowledgeSourceDialog";

vi.mock("../api/knowledgeSources", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/knowledgeSources")>();
  return { ...actual, addKnowledgeSource: vi.fn(), removeKnowledgeSource: vi.fn() };
});

const KEY = '{"type": "service_account", "private_key": "secret-value"}';
const FOLDER = "1AbCdEfGhIjKlMnOp";

afterEach(() => {
  cleanup();
  vi.mocked(addKnowledgeSource).mockReset();
  vi.mocked(removeKnowledgeSource).mockReset();
});

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

describe("AddSourceDialog", () => {
  it("checks before it adds, and adds only the form that passed", async () => {
    vi.mocked(addKnowledgeSource).mockImplementation(async (_id, _f, dryRun) => addResult(dryRun));
    const onAdded = vi.fn();
    render(<AddSourceDialog open onOpenChange={() => {}} org="o" namespace="code-x" repo="Acme/x" onAdded={onAdded} />);
    const add = screen.getByRole("button", { name: "Add source" });
    expect(add).toBeDisabled();
    expect(screen.getByRole("button", { name: "Check" })).toBeDisabled();
    await fill();
    // The key is a password field: the page never renders it as text.
    expect(screen.getByLabelText(/^Service account key/)).toHaveAttribute("type", "password");
    expect(screen.queryByText(/secret-value/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Check" }));
    expect(await screen.findByText(/lists 12 documents/)).toBeInTheDocument();
    expect(screen.getByText("Storage design")).toBeInTheDocument();
    expect(vi.mocked(addKnowledgeSource).mock.calls[0]?.[2]).toBe(true);
    expect(add).toBeEnabled();

    // A change after the check turns Add off again.
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: "design-notes" } });
    expect(add).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: "design-docs" } });
    expect(add).toBeEnabled();
    fireEvent.click(add);
    await waitFor(() => expect(onAdded).toHaveBeenCalledWith("design-docs"));
    expect(vi.mocked(addKnowledgeSource).mock.calls[1]?.[2]).toBe(false);
    expect(unlabeledControls(document.body)).toEqual([]);
    expect(await axeViolations(document.body)).toEqual([]);
  });

  it("shows a refused check and keeps Add off", async () => {
    vi.mocked(addKnowledgeSource).mockRejectedValue(
      new ApiError({ status: 400, code: "CREDENTIAL_REFUSED", message: "the source refused the credential: http_401" }),
    );
    render(
      <AddSourceDialog open onOpenChange={() => {}} org="o" namespace="code-x" repo="Acme/x" onAdded={() => {}} />,
    );
    await fill();
    fireEvent.click(screen.getByRole("button", { name: "Check" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add source" })).toBeDisabled();
  });
});

describe("RetireSourceDialog", () => {
  it("needs the typed name and keeps the documents unless asked", async () => {
    vi.mocked(removeKnowledgeSource).mockResolvedValue({ name: "design-docs", documents_pending: 0 });
    const onRetired = vi.fn();
    render(
      <RetireSourceDialog
        open
        onOpenChange={() => {}}
        org="o"
        namespace="code-x"
        name="design-docs"
        onRetired={onRetired}
      />,
    );
    const retire = screen.getByRole("button", { name: "Retire source" });
    expect(retire).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/to confirm/), { target: { value: "design-docs" } });
    fireEvent.click(retire);
    await waitFor(() => expect(onRetired).toHaveBeenCalled());
    expect(removeKnowledgeSource).toHaveBeenCalledWith(
      { org: "o", namespace: "code-x" },
      "design-docs",
      "design-docs",
      false,
    );
  });
});
