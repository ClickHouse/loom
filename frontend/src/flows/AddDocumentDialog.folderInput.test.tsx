/**
 * The Library's add dialog in a browser whose file input takes a folder: "Choose a folder"
 * reads the picked folder's files, with their paths, into the folder upload.
 */
import { fireEvent, render, screen, waitFor } from "../test/rtl";
import { describe, expect, it, vi } from "vitest";

// jsdom's input has no folder picking: the dialog offers "Choose a folder" only where it does.
vi.hoisted(() => {
  Object.defineProperty(HTMLInputElement.prototype, "webkitdirectory", {
    value: false,
    writable: true,
    configurable: true,
  });
});
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  knowledgeFormats: vi.fn(async () => ({
    formats: [{ format: "markdown", extensions: ["md"], media_types: [], available: true }],
    max_upload_bytes: 1_000_000,
    refused_extensions: [],
    default_format: "text",
  })),
  listKnowledge: vi.fn(async () => ({ rows: [], next_cursor: null })),
}));
import { AddDocumentDialog } from "./AddDocumentDialog";

describe("Choose a folder", () => {
  it("reads a picked folder's files into the folder upload", async () => {
    const team = { namespace: "col-team", parent: "", personal: false, role: "writer" as const, title: "Team" };
    render(
      <AddDocumentDialog
        open
        onOpenChange={() => {}}
        identity={{ org: "o", namespace: "col-team" }}
        place="Team"
        tree={{ rows: [team], shown: team, onMade: () => {} }}
        onAdded={() => {}}
      />,
    );
    expect(await screen.findByRole("button", { name: "Choose a folder" })).toBeInTheDocument();
    const files = ["Q3/a.md", "Q3/Drafts/b.md"].map((p) => {
      const f = new File(["x"], p.split("/").pop() as string);
      Object.defineProperty(f, "webkitRelativePath", { value: p });
      return f;
    });
    fireEvent.change(screen.getByLabelText("Choose a folder to upload"), { target: { files } });
    await waitFor(() => expect(screen.getByText("Creates 2 collections in Team:")).toBeInTheDocument());
  });
});
