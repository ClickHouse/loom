import { act, cleanup, render, screen, waitFor } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../api/guardrails";
import type { ExtractionPolicy, OrgModelCalls } from "../../api/guardrails";
import { confirmAction } from "../../components/ConfirmDialog";
import { toast } from "../../components/Toast";
import { useStore } from "../../lib/state";
import { OrgGuardrails } from "./OrgGuardrails";

vi.mock("../../components/ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true) }));
vi.mock("../../components/Toast", () => ({ toast: vi.fn(), toastError: vi.fn(), ToastViewport: () => null }));
vi.mock("../../api/guardrails", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/guardrails")>();
  return {
    ...actual,
    getExtractionPolicy: vi.fn(),
    setExtractionPolicy: vi.fn(),
    clearExtractionPolicy: vi.fn(),
    getOrgModelCalls: vi.fn(),
    setOrgModelCalls: vi.fn(),
  };
});

const policy = (text: string): ExtractionPolicy => ({
  policy: text,
  updated_by: text ? "alice" : "",
  updated_at: null,
});
const CALLS: OrgModelCalls = {
  enabled: true,
  reason: "",
  updated_by: "",
  updated_at: null,
  locked: false,
  locked_by: "",
};
const scope = { org: "acme", namespace: "team" };

beforeEach(() => {
  useStore.setState({ identity: { ...scope, agent: "loom-ui" } });
  vi.mocked(api.getExtractionPolicy).mockImplementation(async (_id, s) =>
    policy(s === "org" ? "Never store card numbers." : ""),
  );
  vi.mocked(api.getOrgModelCalls).mockResolvedValue(CALLS);
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  useStore.setState({ whoami: null });
});

describe("OrgGuardrails", () => {
  it("saves the namespace's rules and removes the org's", async () => {
    vi.mocked(api.setExtractionPolicy).mockResolvedValue(policy("No health data."));
    vi.mocked(api.clearExtractionPolicy).mockResolvedValue(policy(""));
    render(<OrgGuardrails />);
    const fields = await screen.findAllByLabelText(/^Rules for/);
    expect(fields[0]).toHaveValue("Never store card numbers.");
    await userEvent.type(fields[1], "No health data.");
    await userEvent.click(screen.getAllByRole("button", { name: "Save rules" })[1]);
    await waitFor(() =>
      expect(api.setExtractionPolicy).toHaveBeenCalledWith(
        expect.objectContaining(scope),
        "namespace",
        "No health data.",
      ),
    );
    // The org's rules are set, so its panel offers Remove; the namespace's only once saved.
    await userEvent.click(screen.getAllByRole("button", { name: "Remove" })[0]);
    expect(confirmAction).toHaveBeenCalledWith(expect.objectContaining({ confirmLabel: "Remove", danger: true }));
    await waitFor(() => expect(api.clearExtractionPolicy).toHaveBeenCalledWith(expect.objectContaining(scope), "org"));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Rules removed for acme", "ok"));
  });

  it("removes no rules when the confirmation is declined", async () => {
    vi.mocked(confirmAction).mockResolvedValueOnce(false);
    render(<OrgGuardrails />);
    await screen.findAllByLabelText(/^Rules for/);
    await userEvent.click(screen.getAllByRole("button", { name: "Remove" })[0]);
    await waitFor(() => expect(confirmAction).toHaveBeenCalled());
    expect(api.clearExtractionPolicy).not.toHaveBeenCalled();
  });

  it("keeps a save that lands after a namespace switch out of the new namespace's rules", async () => {
    let answer: (p: ExtractionPolicy) => void = () => {};
    vi.mocked(api.setExtractionPolicy).mockImplementationOnce(() => new Promise((r) => (answer = r)));
    render(<OrgGuardrails />);
    const field = (await screen.findAllByLabelText(/^Rules for/))[1];
    await userEvent.type(field, "No health data.");
    await userEvent.click(screen.getAllByRole("button", { name: "Save rules" })[1]);
    vi.mocked(api.getExtractionPolicy).mockImplementation(async (_id, s) =>
      policy(s === "org" ? "Never store card numbers." : "Other team's rule."),
    );
    act(() => useStore.setState({ identity: { ...scope, namespace: "other", agent: "loom-ui" } }));
    expect(await screen.findByLabelText("Rules for other")).toHaveValue("Other team's rule.");
    await act(async () => answer(policy("No health data.")));
    // The late answer names where it landed.
    expect(toast).toHaveBeenCalledWith("Rules saved for team", "ok");
    expect(screen.getByLabelText("Rules for other")).toHaveValue("Other team's rule.");
  });

  it("switches the org's model calls off only after a confirmation", async () => {
    vi.mocked(api.setOrgModelCalls).mockResolvedValue({ ...CALLS, enabled: false });
    render(<OrgGuardrails />);
    await userEvent.click(await screen.findByRole("switch", { name: "Model calls" }));
    expect(confirmAction).toHaveBeenCalledWith(expect.objectContaining({ danger: true }));
    await waitFor(() => expect(api.setOrgModelCalls).toHaveBeenCalledWith("acme", false, ""));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Model calls off for acme", "ok"));
    expect(await screen.findByText("Off")).toBeInTheDocument();
  });

  it("lets a platform admin switch calls back on under the lock", async () => {
    useStore.setState({ whoami: { authenticated: true, subject: "root", org: "*", roles: ["platform_admin"] } });
    vi.mocked(api.getOrgModelCalls).mockResolvedValue({ ...CALLS, enabled: false, locked: true, locked_by: "root" });
    render(<OrgGuardrails />);
    expect(await screen.findByRole("switch", { name: "Model calls" })).toBeEnabled();
    expect(screen.queryByText(/only they can switch them on/)).toBeNull();
  });

  it("says a failed read failed, with Retry", async () => {
    vi.mocked(api.getOrgModelCalls).mockRejectedValueOnce(new Error("boom"));
    render(<OrgGuardrails />);
    await userEvent.click(await screen.findByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("switch", { name: "Model calls" })).toBeInTheDocument();
  });

  it("lets an org admin switch calls off under the lock while they are on", async () => {
    vi.mocked(api.getOrgModelCalls).mockResolvedValue({ ...CALLS, enabled: true, locked: true, locked_by: "root" });
    render(<OrgGuardrails />);
    expect(await screen.findByRole("switch", { name: "Model calls" })).toBeEnabled();
    expect(screen.getByLabelText("Reason (sent with the switch)")).toBeInTheDocument();
  });

  it("holds the switch under a platform admin's lock", async () => {
    vi.mocked(api.getOrgModelCalls).mockResolvedValue({ ...CALLS, enabled: false, locked: true, locked_by: "root" });
    render(<OrgGuardrails />);
    expect(await screen.findByRole("switch", { name: "Model calls" })).toBeDisabled();
    expect(screen.getByText(/only they can switch them on/)).toBeInTheDocument();
  });
});
