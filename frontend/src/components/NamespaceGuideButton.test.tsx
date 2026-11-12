/** The Memories header's Guide button: always there, described by what the guide is, and a
 *  writer of a namespace without one lands in the editor on an outline. */
import { describe, it, expect, vi, beforeEach } from "vitest";
import userEvent from "@testing-library/user-event";
import { render, screen, fireEvent, waitFor } from "../test/rtl";
import { NamespaceGuideButton } from "./NamespaceGuideButton";
import * as api from "../api";
import { useNamespaceRoleRead } from "../lib/namespaceRole";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, getNamespaceGuide: vi.fn(), saveNamespaceGuide: vi.fn() };
});
vi.mock("../lib/namespaceRole", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/namespaceRole")>();
  return { ...actual, useNamespaceRoleRead: vi.fn() };
});

const NONE: api.NamespaceGuide = {
  found: false,
  revision: 0,
  content: "",
  summary: "",
  note: "",
  updated_by: "",
  updated_at: null,
};
const GUIDE: api.NamespaceGuide = {
  ...NONE,
  found: true,
  revision: 3,
  content: "## Keys\n- acct/<id>/plan",
  summary: "Per-account plans",
  updated_by: "human:alice",
  updated_at: "2026-10-21T10:00:00Z",
};
const identity = { org: "o", namespace: "billing" };
const guideButton = () => screen.getByRole("button", { name: "Guide" });

beforeEach(() => {
  vi.mocked(api.getNamespaceGuide).mockReset();
  vi.mocked(api.saveNamespaceGuide).mockReset();
  vi.mocked(useNamespaceRoleRead).mockReturnValue({ role: "writer", loading: false, rows: [] });
});

describe("NamespaceGuideButton", () => {
  it("is described by the guide's revision and summary, and opens it", async () => {
    vi.mocked(api.getNamespaceGuide).mockResolvedValue(GUIDE);
    vi.mocked(useNamespaceRoleRead).mockReturnValue({ role: "viewer", loading: false, rows: [] });
    render(<NamespaceGuideButton identity={identity} />);
    await waitFor(() => expect(guideButton()).toHaveAccessibleDescription("Revision 3: Per-account plans"));
    fireEvent.click(guideButton());
    expect(await screen.findByText("## Keys\n- acct/<id>/plan", { normalizer: (s) => s })).toBeInTheDocument();
  });

  it("tells a writer there is none, and opens the editor on an outline that cannot be saved as it is", async () => {
    vi.mocked(api.getNamespaceGuide).mockResolvedValue(NONE);
    render(<NamespaceGuideButton identity={identity} />);
    await waitFor(() => expect(guideButton()).toHaveAccessibleDescription(/^No guide yet: write/));
    fireEvent.click(guideButton());
    const editor = await screen.findByLabelText("Guide (Markdown)");
    expect((editor as HTMLTextAreaElement).value).toContain("## What this namespace holds");
    expect(screen.getByRole("button", { name: "Save" })).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(api.saveNamespaceGuide).not.toHaveBeenCalled();
  });

  it("tells a reader there is none, and shows Write unavailable, opening no editor", async () => {
    vi.mocked(api.getNamespaceGuide).mockResolvedValue(NONE);
    vi.mocked(useNamespaceRoleRead).mockReturnValue({ role: "viewer", loading: false, rows: [] });
    render(<NamespaceGuideButton identity={identity} />);
    await waitFor(() => expect(guideButton()).toHaveAccessibleDescription("No guide yet"));
    fireEvent.click(guideButton());
    expect(await screen.findByText(/No guide yet\. Agents that write here/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Guide (Markdown)")).toBeNull();
    const write = screen.getByRole("button", { name: "Write the guide" });
    expect(write).toHaveAccessibleDescription("Needs writer on billing. An admin of it can grant that.");
    fireEvent.click(write);
    expect(screen.queryByLabelText("Guide (Markdown)")).toBeNull();
  });

  it("stays in place, and keeps its answer, while a save's re-read runs", async () => {
    // The button's read, the dialog's, then the button's re-read after the save, which never answers.
    vi.mocked(api.getNamespaceGuide)
      .mockResolvedValueOnce(GUIDE)
      .mockResolvedValueOnce(GUIDE)
      .mockReturnValueOnce(new Promise(() => {}));
    vi.mocked(api.saveNamespaceGuide).mockResolvedValue({ written: true, conflict: false, revision: 4, current: null });
    render(<NamespaceGuideButton identity={identity} />);
    await waitFor(() => expect(guideButton()).toHaveAccessibleDescription("Revision 3: Per-account plans"));
    fireEvent.click(guideButton());
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    await userEvent.type(screen.getByLabelText("Guide (Markdown)"), " more");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.getNamespaceGuide).toHaveBeenCalledTimes(3));
    expect(guideButton()).toHaveAccessibleDescription("Revision 3: Per-account plans");
  });

  it("forgets the last namespace's guide the moment another namespace is shown", async () => {
    vi.mocked(api.getNamespaceGuide)
      .mockResolvedValueOnce(GUIDE)
      .mockReturnValueOnce(new Promise(() => {}));
    const { rerender } = render(<NamespaceGuideButton identity={identity} />);
    await waitFor(() => expect(guideButton()).toHaveAccessibleDescription("Revision 3: Per-account plans"));
    rerender(<NamespaceGuideButton identity={{ org: "o", namespace: "other" }} />);
    expect(guideButton()).toHaveAccessibleDescription("How this namespace is laid out, for agents and people");
  });

  it("is an icon named Guide where the title row is narrow", async () => {
    vi.mocked(api.getNamespaceGuide).mockResolvedValue(GUIDE);
    render(<NamespaceGuideButton identity={identity} compact />);
    await waitFor(() => expect(guideButton()).toHaveAccessibleDescription(/Revision 3: Per-account plans/));
    expect(guideButton()).not.toHaveTextContent("Guide");
  });

  it("opens a writer on the read view when the namespace has a guide", async () => {
    vi.mocked(api.getNamespaceGuide).mockResolvedValue(GUIDE);
    render(<NamespaceGuideButton identity={identity} />);
    await waitFor(() => expect(guideButton()).toHaveAccessibleDescription("Revision 3: Per-account plans"));
    fireEvent.click(guideButton());
    expect(await screen.findByRole("button", { name: "Edit" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Guide (Markdown)")).toBeNull();
  });

  it("is not there on an end user's personal collection, which takes no guide", async () => {
    vi.mocked(api.getNamespaceGuide).mockResolvedValue(NONE);
    const { container } = render(<NamespaceGuideButton identity={{ org: "o", namespace: "col-me-alice" }} />);
    expect(container).toBeEmptyDOMElement();
    await new Promise((r) => setTimeout(r, 0));
    expect(api.getNamespaceGuide).not.toHaveBeenCalled();
  });

  it("offers no editor while the caller's role is unknown", async () => {
    vi.mocked(api.getNamespaceGuide).mockResolvedValue(NONE);
    vi.mocked(useNamespaceRoleRead).mockReturnValue({ role: null, loading: false, rows: [] });
    render(<NamespaceGuideButton identity={identity} />);
    await waitFor(() => expect(guideButton()).toHaveAccessibleDescription("No guide yet"));
    fireEvent.click(guideButton());
    expect(await screen.findByText(/No guide yet\. Agents that write here/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Guide (Markdown)")).toBeNull();
    // Before whoami is read, Write says the role is still loading, not the role it needs.
    expect(screen.getByRole("button", { name: "Write the guide" })).toHaveAccessibleDescription(
      "Your role on this namespace is still loading.",
    );
  });

  it("keeps the outline unsaved when only whitespace was added", async () => {
    vi.mocked(api.getNamespaceGuide).mockResolvedValue(NONE);
    render(<NamespaceGuideButton identity={identity} />);
    await waitFor(() => expect(guideButton()).toHaveAccessibleDescription(/^No guide yet: write/));
    fireEvent.click(guideButton());
    const editor = await screen.findByLabelText("Guide (Markdown)");
    await userEvent.type(editor, "{Enter}{Enter}  ");
    expect(screen.getByRole("button", { name: "Save" })).toHaveAttribute("aria-disabled", "true");
    // A blank line inside the outline is still the outline.
    const text = (editor as HTMLTextAreaElement).value;
    fireEvent.change(editor, { target: { value: text.replace("## Keys", "\n\n## Keys") } });
    expect(screen.getByRole("button", { name: "Save" })).toHaveAttribute("aria-disabled", "true");
    await userEvent.type(editor, "Keys: acct/{{}id}/plan");
    expect(screen.getByRole("button", { name: "Save" })).not.toHaveAttribute("aria-disabled");
  });

  it("keeps its neutral description when the read fails, and the dialog says why", async () => {
    vi.mocked(api.getNamespaceGuide).mockRejectedValue(new Error("the guide service is down"));
    render(<NamespaceGuideButton identity={identity} />);
    await waitFor(() => expect(api.getNamespaceGuide).toHaveBeenCalledTimes(1));
    expect(guideButton()).toHaveAccessibleDescription("How this namespace is laid out, for agents and people");
    fireEvent.click(guideButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(/the guide service is down/);
  });
});
