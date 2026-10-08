import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyNamespaceEntry, WhoAmI } from "../api";
import { ApiError } from "../lib/errors";
import { useStore } from "../lib/state";
import { invalidateShared } from "../lib/sharedReads";
import { NamespacePicker } from "./NamespacePicker";
import { ToastViewport } from "./Toast";

// Withdrawing a pending request that the server refuses raises the standard error
// toast: the humanized reason plus the request id to quote, for any thrown value.

const pending: MyNamespaceEntry = {
  namespace: "team",
  role: "viewer",
  status: "pending",
  pending_role: "writer",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
};

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    isAuthDisabled: () => false,
    listMine: vi.fn(async () => [pending]),
    cancelNamespaceRequest: vi.fn(),
  };
});
vi.mock("./ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true) }));
import { cancelNamespaceRequest } from "../api";

const whoami: WhoAmI = {
  authenticated: true,
  subject: "v@example.com",
  org: "dev",
  roles: ["VIEWER"],
  namespaces: ["team"],
};

async function withdraw() {
  render(
    <>
      <NamespacePicker />
      <ToastViewport />
    </>,
  );
  fireEvent.click(screen.getByRole("button", { name: /team/ }));
  fireEvent.click(await screen.findByRole("button", { name: /^pending writer$/ }));
}

describe("NamespacePicker withdraw failure", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    invalidateShared();
    useStore.setState({
      authed: true,
      whoami,
      myNamespaces: [pending],
      identity: { org: "dev", namespace: "team", agent: "loom-ui" },
    });
  });
  afterEach(() => cleanup());

  it("shows the humanized reason and the request id of an API error", async () => {
    vi.mocked(cancelNamespaceRequest).mockRejectedValueOnce(
      new ApiError({ status: 500, message: "boom", requestId: "rid-42" }),
    );
    await withdraw();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Cancel failed:");
    expect(alert.textContent).toContain("boom");
    expect(alert.textContent).toContain("request id rid-42");
  });

  it("names a non-Error throw instead of printing undefined", async () => {
    vi.mocked(cancelNamespaceRequest).mockRejectedValueOnce("offline");
    await withdraw();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Cancel failed: offline");
    expect(alert.textContent).not.toContain("undefined");
  });
});
