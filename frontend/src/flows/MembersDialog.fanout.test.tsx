/** From the bell, one Approve / Reject covers every branch of the repository where the
 *  requester is waiting, and the heading names the repository — nothing the approver
 *  reads truncates. */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "../test/rtl";
import { MembersDialog } from "./MembersDialog";
import { useStore } from "../lib/state";
import * as api from "../api";
import { ApiError } from "../lib/errors";
import { ToastViewport } from "../components/Toast";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listGrants: vi.fn(async () => ({
      rows: [
        {
          subject: "reporter-private",
          role: "viewer",
          status: "pending",
          pending_since: "2026-09-17T14:50:28Z",
          granted_at: "2026-09-17T14:50:28Z",
          granted_by: "reporter-private",
        },
      ],
    })),
    approveGrant: vi.fn(async () => ({})),
    createGrant: vi.fn(async () => ({})),
  };
});

const noop = () => {};
const elsewhere = {
  "reporter-private": [
    { namespace: "code-example-private-26-6", label: "ClickHouse/example-private @ release/26.6" },
    { namespace: "code-example-private-26-7", label: "ClickHouse/example-private @ release/26.7" },
  ],
};

beforeEach(() => {
  vi.mocked(api.approveGrant).mockClear();
  vi.mocked(api.createGrant).mockClear();
  useStore.setState({
    whoami: { authenticated: true, subject: "alice", org: "clickhouse", roles: ["ADMIN"] },
  });
});

describe("MembersDialog approves a requester on every waiting branch at once", () => {
  it("heading names the repository and branch, with the identifier beneath; Approve fans out to the siblings", async () => {
    render(
      <MembersDialog
        open
        onOpenChange={noop}
        org="clickhouse"
        namespace="code-example-private-26-5"
        kind="code"
        label="ClickHouse/example-private @ release/26.5"
        pendingElsewhere={elsewhere}
      />,
    );
    expect(
      await screen.findByRole("dialog", { name: /Readers of ClickHouse\/example-private @ release\/26\.5/ }),
    ).toBeInTheDocument();
    expect(screen.getByText("code-example-private-26-5")).toBeInTheDocument();
    expect(await screen.findByText(/also waiting on .*26\.6.*26\.7.*one decision covers all/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Approve as reader on 3 branches/ }));
    await waitFor(() => expect(api.approveGrant).toHaveBeenCalledTimes(3));
    const targets = vi
      .mocked(api.approveGrant)
      .mock.calls.map((c) => c[0].namespace)
      .sort();
    expect(targets).toEqual(["code-example-private-26-5", "code-example-private-26-6", "code-example-private-26-7"]);
    for (const c of vi.mocked(api.approveGrant).mock.calls)
      expect([c[1], c[2]]).toEqual(["reporter-private", "approve"]);
  });

  it("Reject fans out the same way", async () => {
    render(
      <MembersDialog
        open
        onOpenChange={noop}
        org="clickhouse"
        namespace="code-example-private-26-5"
        kind="code"
        pendingElsewhere={elsewhere}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: /^Reject$/ }));
    await waitFor(() => expect(api.approveGrant).toHaveBeenCalledTimes(3));
    for (const c of vi.mocked(api.approveGrant).mock.calls) expect(c[2]).toBe("reject");
  });

  it("without siblings the decision stays on this namespace alone and the heading falls back to the identifier", async () => {
    render(<MembersDialog open onOpenChange={noop} org="clickhouse" namespace="code-example-private" kind="code" />);
    expect(await screen.findByRole("dialog", { name: /Readers of code-example-private/ })).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: /^Approve as reader$/ }));
    await waitFor(() => expect(api.approveGrant).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.approveGrant).mock.calls[0][0]).toEqual({
      org: "clickhouse",
      namespace: "code-example-private",
    });
  });

  it("a second reader added on a code index goes out as the Reader the select still shows", async () => {
    render(<MembersDialog open onOpenChange={noop} org="clickhouse" namespace="code-example-private" kind="code" />);
    for (const [n, subject] of [
      [1, "reader-a"],
      [2, "reader-b"],
    ] as const) {
      const addButtons = await screen.findAllByRole("button", { name: /Add reader/ });
      fireEvent.click(addButtons[addButtons.length - 1]);
      expect(screen.getByRole("button", { name: "Role for the new member" })).toHaveTextContent("Reader");
      fireEvent.change(screen.getByRole("textbox", { name: /member to add/ }), { target: { value: subject } });
      fireEvent.click(screen.getByRole("button", { name: /^Add$/ }));
      await waitFor(() => expect(api.createGrant).toHaveBeenCalledTimes(n));
    }
    expect(vi.mocked(api.createGrant).mock.calls.map((c) => [c[1], c[2]])).toEqual([
      ["reader-a", "viewer"],
      ["reader-b", "viewer"],
    ]);
  });

  it("says a refusal of every branch as one failure", async () => {
    const refused = () =>
      new ApiError({ status: 403, code: "AUTH_INSUFFICIENT", message: "admin needed", detail: { category: "auth" } });
    vi.mocked(api.approveGrant).mockRejectedValueOnce(refused());
    render(
      <>
        <ToastViewport />
        <MembersDialog open onOpenChange={noop} org="clickhouse" namespace="code-example-private" kind="code" />
      </>,
    );
    fireEvent.click(await screen.findByRole("button", { name: /^Approve as reader$/ }));
    expect(
      await screen.findByText("Couldn't approve reporter-private: Your role doesn't allow this — admin needed."),
    ).toBeInTheDocument();
  });

  it("says how far a fan-out got before it failed", async () => {
    vi.mocked(api.approveGrant)
      .mockResolvedValueOnce({} as never)
      .mockRejectedValueOnce(new ApiError({ status: 403, code: "AUTH_INSUFFICIENT", message: "admin needed" }));
    render(
      <>
        <ToastViewport />
        <MembersDialog
          open
          onOpenChange={noop}
          org="clickhouse"
          namespace="code-example-private-26-5"
          kind="code"
          pendingElsewhere={elsewhere}
        />
      </>,
    );
    fireEvent.click(await screen.findByRole("button", { name: /Approve as reader on 3 branches/ }));
    expect(
      await screen.findByText(
        "Approved reporter-private on 2 of 3 branches; the rest failed: Your role doesn't allow this — admin needed.",
      ),
    ).toBeInTheDocument();
  });
});
