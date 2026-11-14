// Move to…: pick a place the person writes, see whose sharing the documents take, move them,
// and hear how many went and why the rest stayed.
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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

function dialog(
  uris = ["doc://a.md", "doc://b.md"],
  places = PLACES,
  placesReading: "loading" | "failed" | null = null,
) {
  const onMoved = vi.fn();
  const onClose = vi.fn();
  const view = render(
    <MoveDocumentsDialog
      org="acme"
      from="col-ingest"
      fromLabel="Ingest"
      sourceUris={uris}
      places={places}
      placesReading={placesReading}
      onMoved={onMoved}
      onClose={onClose}
    />,
  );
  return { onMoved, onClose, unmount: view.unmount };
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
    expect(api.moveKnowledge).toHaveBeenCalledWith(
      { org: "acme", namespace: "col-ingest" },
      "col-storage",
      ["doc://a.md", "doc://b.md"],
      expect.any(AbortSignal),
    );
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
    // The list is read again whatever the answer.
    expect(onMoved).toHaveBeenCalledWith([]);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("words a failed call and keeps the pick", async () => {
    api.moveKnowledge.mockResolvedValue({
      found: true,
      to_namespace: "col-eng",
      results: [],
      error: new Error("boom"),
    });
    const { onMoved } = dialog();
    await pick("Engineering");
    await userEvent.click(screen.getByRole("button", { name: "Move" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Move" })).toBeEnabled();
    expect(onMoved).toHaveBeenCalledWith([]);
  });

  it("says what moved before a later call failed", async () => {
    api.moveKnowledge.mockResolvedValue({
      found: true,
      to_namespace: "col-eng",
      results: [{ source_uri: "doc://a.md", moved: true, reason: "" }],
      error: new Error("boom"),
    });
    const { onMoved, onClose } = dialog();
    await pick("Engineering");
    await userEvent.click(screen.getByRole("button", { name: "Move" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /1 document moved before that\. Some documents of the last call may have moved too; the list shows where each one is now\.$/,
    );
    expect(onMoved).toHaveBeenCalledWith(["doc://a.md"]);
    expect(onClose).not.toHaveBeenCalled();
    // Move again sends only what is left.
    expect(screen.getByRole("dialog", { name: "Move 1 document" })).toBeInTheDocument();
    api.moveKnowledge.mockResolvedValue({ found: true, to_namespace: "col-eng", results: [] });
    await userEvent.click(screen.getByRole("button", { name: "Move" }));
    expect(api.moveKnowledge).toHaveBeenLastCalledWith(
      { org: "acme", namespace: "col-ingest" },
      "col-eng",
      ["doc://b.md"],
      expect.any(AbortSignal),
    );
  });

  it("stops the calls still to send once it closes mid-move", async () => {
    let signal: AbortSignal | undefined;
    let answer: (v: unknown) => void = () => {};
    api.moveKnowledge.mockImplementation((_id: unknown, _to: unknown, _uris: unknown, s: AbortSignal) => {
      signal = s;
      return new Promise((resolve) => {
        answer = resolve;
      });
    });
    const { onMoved, onClose, unmount } = dialog();
    await pick("Engineering");
    await userEvent.click(screen.getByRole("button", { name: "Move" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalled();
    unmount();
    expect(signal?.aborted).toBe(true);
    // The call in flight still answers: the list is read again, nothing else happens.
    answer({ found: true, to_namespace: "col-eng", results: [{ source_uri: "doc://a.md", moved: true, reason: "" }] });
    await waitFor(() => expect(onMoved).toHaveBeenCalledWith(["doc://a.md"]));
    expect(toast).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("says the collections are still being read, or could not be, instead of no place", () => {
    dialog(["doc://a.md"], [], "loading");
    expect(screen.getByText("Reading the collections…")).toBeInTheDocument();
    expect(screen.queryByText(/no other place/)).not.toBeInTheDocument();
    cleanup();
    dialog(["doc://a.md"], [], "failed");
    expect(screen.getByText("The collections could not be read, so they are not listed here.")).toBeInTheDocument();
    cleanup();
    dialog(["doc://a.md"], []);
    expect(screen.getByText("There is no other place you can add documents to.")).toBeInTheDocument();
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
