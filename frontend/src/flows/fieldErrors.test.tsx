import { render, screen } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { AddMemoryDialog } from "./AddMemoryDialog";
import { CopyToNamespaceDialog } from "./CopyToNamespaceDialog";
import { CreateNamespaceDialog } from "./CreateNamespaceDialog";
import { InboxSettingsDialog } from "./InboxSettingsDialog";
import { MembersDialog } from "./MembersDialog";

// Click UI draws a field's error as a bare <div> beside the input. Each dialog's field
// with an error is marked invalid and described by the message, so a screen reader says
// why when it lands on the field.

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listMine: vi.fn(async () => []),
    listGrants: vi.fn(async () => ({ rows: [] })),
    createGrant: vi.fn(async () => {
      throw new Error("No such subject");
    }),
  };
});
vi.mock("../api/tasks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/tasks")>();
  return {
    ...actual,
    listInboxes: vi.fn(async () => []),
    listPrincipals: vi.fn(async () => ({ principals: [], truncated: false })),
    listPolicies: vi.fn(async () => []),
  };
});

const identity = { org: "dev", namespace: "team", agent: "dashboard" };
const noop = () => undefined;

beforeEach(() => {
  useStore.setState({
    whoami: { authenticated: true, subject: "op@example.com", org: "dev", roles: ["ADMIN"] },
    identity,
  });
});

async function expectInvalid(field: HTMLElement, message: RegExp) {
  expect(await screen.findByText(message)).toBeInTheDocument();
  expect(field).toHaveAttribute("aria-invalid", "true");
  expect(field).toHaveAccessibleDescription(message);
}

describe("a field with an error is invalid and described by it", () => {
  it("AddMemoryDialog's key", async () => {
    render(<AddMemoryDialog open onOpenChange={noop} identity={identity} onAdded={noop} />);
    const key = screen.getByRole("textbox", { name: /^Key/ });
    expect(key).not.toHaveAttribute("aria-invalid");
    await userEvent.setup().type(key, "Bad Key");
    await expectInvalid(key, /lower_snake_case, start with a letter/);
  });

  it("CopyToNamespaceDialog's destination key", async () => {
    render(
      <CopyToNamespaceDialog open onOpenChange={noop} source={identity} memoryKey="user_coffee" onCopied={noop} />,
    );
    const key = screen.getByRole("textbox", { name: /^Key in destination/ });
    await userEvent.setup().type(key, "Bad Key");
    await expectInvalid(key, /lower_snake_case, start with a letter/);
  });

  it("CreateNamespaceDialog's name", async () => {
    render(<CreateNamespaceDialog open onOpenChange={noop} onCreated={noop} />);
    const name = screen.getByRole("textbox", { name: /^Name/ });
    await userEvent.setup().type(name, "Bad Name");
    await expectInvalid(name, /lowercase letters, digits, and dashes only/);
  });

  it("InboxSettingsDialog's inbox name", async () => {
    render(<InboxSettingsDialog open onOpenChange={noop} identity={identity} inbox={null} onSaved={noop} />);
    const name = screen.getByRole("textbox", { name: /^Inbox/ });
    await userEvent.setup().type(name, "-bad");
    await expectInvalid(name, /starts with a letter or digit/);
  });

  it("MembersDialog's new member, after the server refuses it", async () => {
    const user = userEvent.setup();
    render(<MembersDialog open onOpenChange={noop} org="dev" namespace="team" kind="memory" />);
    await user.click((await screen.findAllByRole("button", { name: /Add member/ }))[0]);
    const subject = await screen.findByRole("textbox", { name: /member to add/ });
    await user.type(subject, "nobody@example.com{Enter}");
    await expectInvalid(subject, /No such subject/);
  });
});
