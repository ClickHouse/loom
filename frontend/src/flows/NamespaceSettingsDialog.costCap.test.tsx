/** The daily cost-cap field saves once, on blur or Enter; partial or invalid text never
 *  reaches the server, and Escape restores the stored cap without closing the dialog. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "../test/rtl";
import { NamespaceSettingsDialog } from "./NamespaceSettingsDialog";
import { useStore } from "../lib/state";
import * as api from "../api";
import type { NamespaceInfo } from "../api";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, getNamespace: vi.fn(), reregisterNamespace: vi.fn() };
});

const row = (namespace: string, cap: number): NamespaceInfo => ({
  namespace,
  owner_email: "me@example.com",
  description: "",
  tags: [],
  active: true,
  consolidation_enabled: false,
  conflict_threshold: null,
  daily_cost_cap_usd: cap,
  created_at: "",
  updated_at: "",
});

beforeEach(() => {
  vi.mocked(api.getNamespace).mockReset();
  vi.mocked(api.reregisterNamespace).mockReset();
  vi.mocked(api.reregisterNamespace).mockResolvedValue({ registered: true, namespace: "ns", updated_at: "" });
  useStore.setState({ whoami: { authenticated: true, subject: "me", org: "dev", roles: [] }, myNamespaces: null });
});

async function openDialog(onOpenChange: (v: boolean) => void = () => undefined) {
  vi.mocked(api.getNamespace).mockResolvedValue(row("ns", 5));
  const view = render(<NamespaceSettingsDialog open onOpenChange={onOpenChange} org="dev" namespace="ns" />);
  await act(async () => undefined);
  const input = screen.getByLabelText("Daily cost cap (USD)");
  if (!(input instanceof HTMLInputElement)) throw new Error("cost-cap field is not an input");
  input.focus();
  return { input, ...view };
}

describe("NamespaceSettingsDialog cost cap", () => {
  it("typing sends nothing; blur commits the final value once", async () => {
    const { input } = await openDialog();
    fireEvent.change(input, { target: { value: "1" } });
    fireEvent.change(input, { target: { value: "12" } });
    expect(api.reregisterNamespace).not.toHaveBeenCalled();
    expect(input.value).toBe("12");
    await act(async () => fireEvent.blur(input));
    expect(api.reregisterNamespace).toHaveBeenCalledTimes(1);
    expect(api.reregisterNamespace).toHaveBeenCalledWith({ org: "dev", namespace: "ns" }, row("ns", 5), {
      daily_cost_cap_usd: 12,
    });
  });

  it("Enter commits", async () => {
    const { input } = await openDialog();
    fireEvent.change(input, { target: { value: "7.5" } });
    await act(async () => fireEvent.keyDown(input, { key: "Enter" }));
    expect(api.reregisterNamespace).toHaveBeenCalledTimes(1);
    expect(api.reregisterNamespace).toHaveBeenCalledWith({ org: "dev", namespace: "ns" }, row("ns", 5), {
      daily_cost_cap_usd: 7.5,
    });
  });

  it.each(["-1", "", "5"])("%j sends nothing and reverts to the stored cap", async (text) => {
    const { input } = await openDialog();
    fireEvent.change(input, { target: { value: text } });
    await act(async () => fireEvent.blur(input));
    expect(api.reregisterNamespace).not.toHaveBeenCalled();
    expect(input.value).toBe("5");
  });

  it("Escape restores the stored cap and keeps the dialog open; the next Escape closes it", async () => {
    const onOpenChange = vi.fn();
    const { input } = await openDialog(onOpenChange);
    fireEvent.change(input, { target: { value: "40" } });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(input.value).toBe("5");
    expect(onOpenChange).not.toHaveBeenCalled();
    await act(async () => fireEvent.blur(input));
    expect(api.reregisterNamespace).not.toHaveBeenCalled();
    input.focus();
    fireEvent.keyDown(input, { key: "Escape" });
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("a draft typed for one namespace is not shown under the next", async () => {
    const { input, rerender } = await openDialog();
    fireEvent.change(input, { target: { value: "40" } });
    vi.mocked(api.getNamespace).mockResolvedValue(row("other", 3));
    rerender(<NamespaceSettingsDialog open onOpenChange={() => undefined} org="dev" namespace="other" />);
    await act(async () => undefined);
    expect(screen.getByLabelText("Daily cost cap (USD)")).toHaveValue(3);
  });

  it("a draft abandoned by closing is gone on reopen and never sent", async () => {
    const { input, rerender } = await openDialog();
    fireEvent.change(input, { target: { value: "40" } });
    rerender(<NamespaceSettingsDialog open={false} onOpenChange={() => undefined} org="dev" namespace="ns" />);
    await act(async () => undefined);
    rerender(<NamespaceSettingsDialog open onOpenChange={() => undefined} org="dev" namespace="ns" />);
    await act(async () => undefined);
    const reopened = screen.getByLabelText("Daily cost cap (USD)");
    expect(reopened).toHaveValue(5);
    reopened.focus();
    await act(async () => fireEvent.blur(reopened));
    expect(api.reregisterNamespace).not.toHaveBeenCalled();
  });
});
