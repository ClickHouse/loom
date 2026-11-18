import { cleanup, render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { axeViolations } from "../test/axe";
import * as api from "../api/sourcePush";
import type { PushStatus } from "../api/sourcePush";
import { SourcePushDialog } from "./SourcePushDialog";

vi.mock("../api/sourcePush", () => ({
  sourcePushStatus: vi.fn(),
  configureSourcePush: vi.fn(),
  confirmSourcePush: vi.fn(),
}));
const source = { org: "org", namespace: "docs", name: "source" };
const disabled: PushStatus = {
  enabled: true,
  status: "disabled",
  provider: "notion",
  generation: "",
  callback_url: "",
  challenge_fingerprint: "",
  verification_token: "",
  channels: 0,
  expires_at: 0,
};
const pending: PushStatus = {
  ...disabled,
  status: "pending",
  generation: "generation",
  callback_url: "https://loom.example/hooks/knowledge/docs/id/generation?org=org",
};

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.sourcePushStatus).mockResolvedValue(disabled);
});
afterEach(cleanup);

describe("SourcePushDialog", () => {
  it("pauses setup when the deployment opts out while keeping Disable available", async () => {
    const user = userEvent.setup();
    vi.mocked(api.sourcePushStatus).mockResolvedValue({ ...pending, enabled: false, status: "active" });
    vi.mocked(api.configureSourcePush).mockResolvedValue({ ...disabled, enabled: false });
    render(<SourcePushDialog {...source} kind="notion" onClose={() => {}} />);
    expect(await screen.findByText("Paused")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start setup" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Disable push" }));
    expect(api.configureSourcePush).toHaveBeenCalledWith(source, false);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Disable push" })).toBeNull());
    expect(screen.queryByRole("button", { name: "Start setup" })).toBeNull();
  });
  it("prepares a Linear address before asking for its secret and confirms the same setup", async () => {
    const user = userEvent.setup();
    vi.mocked(api.configureSourcePush).mockResolvedValue({ ...pending, provider: "linear" });
    vi.mocked(api.confirmSourcePush).mockResolvedValue({ ...pending, provider: "linear", status: "active" });
    render(<SourcePushDialog {...source} kind="linear" onClose={() => {}} />);
    await user.click(await screen.findByRole("button", { name: "Start setup" }));
    expect(api.configureSourcePush).toHaveBeenCalledWith(source, true);
    expect(await screen.findByText(pending.callback_url)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh" })).toHaveFocus();
    await user.type(screen.getByLabelText("Webhook signing secret", { exact: true }), "signing-secret");
    expect(await axeViolations(document.body)).toEqual([]);
    await user.click(screen.getByRole("button", { name: "Enable push" }));
    expect(api.confirmSourcePush).toHaveBeenCalledWith(source, { ...pending, provider: "linear" }, "signing-secret");
    expect(await screen.findByText("Active")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh" })).toHaveFocus();
    expect(screen.queryByDisplayValue("signing-secret")).toBeNull();
  });

  it("reveals a Notion token explicitly, then removes it after confirmation", async () => {
    const user = userEvent.setup();
    const inspected = { ...pending, challenge_fingerprint: "fingerprint" };
    const revealed = { ...inspected, verification_token: "verification-secret" };
    vi.mocked(api.sourcePushStatus).mockResolvedValueOnce(inspected).mockResolvedValueOnce(revealed);
    vi.mocked(api.confirmSourcePush).mockResolvedValue({ ...pending, status: "active" });
    render(<SourcePushDialog {...source} kind="notion" onClose={() => {}} />);
    expect(screen.queryByText("verification-secret")).toBeNull();
    await user.click(await screen.findByRole("button", { name: "Reveal verification token" }));
    expect(api.sourcePushStatus).toHaveBeenLastCalledWith(source, true);
    expect(await screen.findByText("verification-secret")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "I verified in Notion" }));
    expect(api.confirmSourcePush).toHaveBeenCalledWith(source, revealed);
    expect(await screen.findByText("Active")).toBeInTheDocument();
    expect(screen.queryByText("verification-secret")).toBeNull();
    expect(screen.getByRole("button", { name: "Refresh" })).toHaveFocus();
  });

  it.each([
    { status: "expired", enabled: true },
    { status: "expired", enabled: false },
    { status: "obsolete", enabled: true },
    { status: "obsolete", enabled: false },
  ])("retires $status Drive setup with deployment enabled=$enabled", async ({ status, enabled }) => {
    const user = userEvent.setup();
    vi.mocked(api.sourcePushStatus).mockResolvedValue({
      ...disabled,
      provider: "gdrive",
      status,
      enabled,
      generation: status === "expired" ? "generation" : "",
      channels: status === "expired" ? 1 : 0,
      expires_at: status === "expired" ? 1 : 0,
    });
    vi.mocked(api.configureSourcePush).mockResolvedValue({ ...disabled, provider: "gdrive", enabled });
    render(<SourcePushDialog {...source} kind="gdrive" onClose={() => {}} />);
    await screen.findByText(
      enabled ? (status === "expired" ? "Expired; set up again" : "Source changed; set up again") : "Paused",
    );
    expect(await axeViolations(document.body)).toEqual([]);
    await user.click(screen.getByRole("button", { name: "Disable push" }));
    expect(api.configureSourcePush).toHaveBeenCalledWith(source, false);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Disable push" })).toBeNull());
    expect(screen.getByRole("button", { name: "Refresh" })).toHaveFocus();
  });

  it("moves focus to Refresh after disabling push", async () => {
    const user = userEvent.setup();
    vi.mocked(api.sourcePushStatus).mockResolvedValue({ ...pending, status: "active" });
    vi.mocked(api.configureSourcePush).mockResolvedValue(disabled);
    render(<SourcePushDialog {...source} kind="notion" onClose={() => {}} />);
    await user.click(await screen.findByRole("button", { name: "Disable push" }));
    expect(api.configureSourcePush).toHaveBeenCalledWith(source, false);
    expect(await screen.findByText("Disabled")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh" })).toHaveFocus();
  });

  it("drops a late initial read after a refresh and setup complete", async () => {
    const user = userEvent.setup();
    let finishInitial: (status: PushStatus) => void = () => {};
    vi.mocked(api.sourcePushStatus)
      .mockReturnValueOnce(
        new Promise((resolve) => {
          finishInitial = resolve;
        }),
      )
      .mockResolvedValueOnce(disabled);
    vi.mocked(api.configureSourcePush).mockResolvedValue(pending);
    render(<SourcePushDialog {...source} kind="notion" onClose={() => {}} />);
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await user.click(await screen.findByRole("button", { name: "Start setup" }));
    expect(await screen.findByText("Waiting for verification")).toBeInTheDocument();
    finishInitial(disabled);
    await waitFor(() => expect(screen.getByText("Waiting for verification")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Start setup" })).toBeNull();
  });
});
