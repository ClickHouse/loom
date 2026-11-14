// Move to…: pick a place the person writes, see whose sharing the documents take, move them,
// and hear how many went and why the rest stayed.
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../lib/errors";
import { axeViolations } from "../test/axe";
import { cleanup, render, screen, waitFor } from "../test/rtl";
import { MoveDocumentsDialog, moveSummary, type MovePlace } from "./MoveDocumentsDialog";

const api = vi.hoisted(() => ({ moveKnowledge: vi.fn() }));
vi.mock("../api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../api")>()), ...api }));
const toast = vi.hoisted(() => vi.fn());
vi.mock("../components/Toast", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../components/Toast")>()),
  toast,
}));

const PLACES: MovePlace[] = [
  { namespace: "bench", label: "Namespace bench", depth: 0, kind: "namespace" },
  { namespace: "col-me-1", label: "My files", depth: 0, kind: "personal" },
  { namespace: "col-eng", label: "Engineering", depth: 0, kind: "collection" },
  { namespace: "col-storage", label: "Storage", depth: 1, kind: "collection" },
];

beforeEach(() => {
  api.moveKnowledge.mockReset();
  toast.mockReset();
});
afterEach(cleanup);

function dialog(uris = ["doc://a.md", "doc://b.md"]) {
  const onMoved = vi.fn();
  const onClose = vi.fn();
  render(
    <MoveDocumentsDialog
      org="acme"
      from="col-ingest"
      fromLabel="Ingest"
      sourceUris={uris}
      places={PLACES}
      onMoved={onMoved}
      onClose={onClose}
    />,
  );
  return { onMoved, onClose };
}

async function pick(label: string) {
  await userEvent.click(screen.getByRole("button", { name: /To/ }));
  await userEvent.click(await screen.findByText(label));
}

describe("moveSummary", () => {
  it("counts what moved and says why the rest stayed", () => {
    expect(moveSummary([{ source_uri: "a", moved: true, reason: "" }], "Storage")).toEqual({
      text: "Moved 1 document to Storage",
      ok: true,
    });
    expect(
      moveSummary(
        [
          { source_uri: "a", moved: true, reason: "" },
          { source_uri: "b", moved: false, reason: "exists" },
          { source_uri: "c", moved: false, reason: "exists" },
        ],
        "Storage",
      ),
    ).toEqual({
      text: "Moved 1 document to Storage; 2 documents stayed (a document with the same source is already there)",
      ok: false,
    });
    expect(moveSummary([{ source_uri: "a", moved: false, reason: "busy" }], "Storage").text).toBe(
      "Nothing moved: 1 document stayed (being written right now; try again)",
    );
  });
});

describe("MoveDocumentsDialog", () => {
  it("moves to the place picked and says whose sharing they take", async () => {
    api.moveKnowledge.mockResolvedValue({
      found: true,
      to_namespace: "col-storage",
      results: [
        { source_uri: "doc://a.md", moved: true, reason: "" },
        { source_uri: "doc://b.md", moved: true, reason: "" },
      ],
    });
    const { onMoved, onClose } = dialog();
    expect(screen.getByRole("dialog", { name: "Move 2 documents" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Move" })).toBeDisabled();
    await pick("Storage");
    expect(screen.getByText(/They take Storage's sharing/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Move" }));
    expect(api.moveKnowledge).toHaveBeenCalledWith({ org: "acme", namespace: "col-ingest" }, "col-storage", [
      "doc://a.md",
      "doc://b.md",
    ]);
    await waitFor(() => expect(onMoved).toHaveBeenCalledWith(["doc://a.md", "doc://b.md"]));
    expect(toast).toHaveBeenCalledWith("Moved 2 documents to Storage", "ok");
    expect(onClose).toHaveBeenCalled();
  });

  it("says who reads them in the person's own files and in a namespace", async () => {
    dialog(["doc://a.md"]);
    await pick("My files");
    expect(screen.getByText("Only you read them there.")).toBeInTheDocument();
    await pick("Namespace bench");
    expect(screen.getByText("Whoever reads the namespace bench reads them there.")).toBeInTheDocument();
  });

  it("stays open with the reason when the place cannot take them", async () => {
    api.moveKnowledge.mockResolvedValue({ found: false, to_namespace: "", results: [] });
    const { onMoved, onClose } = dialog();
    await pick("Engineering");
    await userEvent.click(screen.getByRole("button", { name: "Move" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("You can no longer add documents to Engineering.");
    expect(onMoved).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("words a failed call and keeps the pick", async () => {
    api.moveKnowledge.mockRejectedValue(new Error("boom"));
    dialog();
    await pick("Engineering");
    await userEvent.click(screen.getByRole("button", { name: "Move" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Move" })).toBeEnabled();
  });

  it("goes on as after a move when the move committed with only lists behind", async () => {
    api.moveKnowledge.mockRejectedValue(
      new ApiError({ status: 503, code: "PUBLICATION_PENDING", detail: { category: "dependency", retryable: false } }),
    );
    const { onMoved, onClose } = dialog();
    await pick("Engineering");
    await userEvent.click(screen.getByRole("button", { name: "Move" }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(onMoved).toHaveBeenCalledWith(["doc://a.md", "doc://b.md"]);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("says what moved before the place closed partway through", async () => {
    api.moveKnowledge.mockResolvedValue({
      found: false,
      to_namespace: "col-eng",
      results: [{ source_uri: "doc://a.md", moved: true, reason: "" }],
    });
    const { onMoved, onClose } = dialog();
    await pick("Engineering");
    await userEvent.click(screen.getByRole("button", { name: "Move" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You can no longer add documents to Engineering. 1 document moved before that.",
    );
    expect(onMoved).toHaveBeenCalledWith(["doc://a.md"]);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("has no axe violations", async () => {
    dialog();
    expect(await axeViolations(screen.getByRole("dialog"))).toEqual([]);
  });
});
