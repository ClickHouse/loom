import { cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { axeViolations } from "../test/axe";
import { ConnectAgentButton } from "./ConnectAgentButton";

// Connecting an agent from an empty page: the dialog opens in place, starts on its
// instructions, holds the token form (or, with auth off, the plugin lines), and hands focus
// back to the button that opened it.

const auth = vi.hoisted(() => ({ off: false }));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, isAuthDisabled: () => auth.off };
});
vi.mock("../views/Tokens", () => ({
  // The token form is the Tokens page's, tested there; here only that it sits in the dialog
  // and that its close closes the dialog.
  QuickConnect: ({ onClose, focusTitle }: { onClose: () => void; focusTitle?: boolean }) => (
    <div>
      Token form{focusTitle === false ? " (no focus)" : ""}
      <button type="button" onClick={onClose}>
        Cancel the form
      </button>
    </div>
  ),
}));

afterEach(() => {
  cleanup();
  auth.off = false;
});

const open = async () => {
  const button = screen.getByRole("button", { name: "Connect an agent" });
  await userEvent.click(button);
  // The dialog's chunk loads on the first press.
  return { button, dialog: await screen.findByRole("dialog", { name: "Connect an agent" }, { timeout: 5000 }) };
};

describe("ConnectAgentButton", () => {
  it("opens the dialog with its title and focuses the instructions", async () => {
    render(<ConnectAgentButton />);
    const { dialog } = await open();
    await waitFor(() => expect(within(dialog).getByText(/Make a token for it below/)).toHaveFocus());
    expect(await within(dialog).findByText("Token form (no focus)")).toBeInTheDocument();
    expect(await axeViolations(dialog)).toEqual([]);
  });

  it("closes when the token form closes, and focus returns to the button", async () => {
    render(<ConnectAgentButton />);
    const { button, dialog } = await open();
    await userEvent.click(await within(dialog).findByRole("button", { name: "Cancel the form" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(button).toHaveFocus());
  });

  it("returns focus to the button on Escape", async () => {
    render(<ConnectAgentButton />);
    const { button } = await open();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(button).toHaveFocus());
  });

  it("with auth off shows the plugin lines, not a token form", async () => {
    auth.off = true;
    render(<ConnectAgentButton type="primary" />);
    const { dialog } = await open();
    const lead = within(dialog).getByText(/no token is needed/);
    await waitFor(() => expect(lead).toHaveFocus());
    expect(within(dialog).getByText(/claude plugin install loom@loom/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Copy commands" })).toBeInTheDocument();
    expect(within(dialog).queryByText(/Token form/)).toBeNull();
  });
});
