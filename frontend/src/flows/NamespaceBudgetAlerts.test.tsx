import { act, fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { NamespaceBudgetAlerts } from "./NamespaceBudgetAlerts";
import type { NamespaceInfo } from "../api";

const namespace: NamespaceInfo = {
  namespace: "n",
  owner_email: "owner@example.com",
  description: "",
  tags: [],
  active: true,
  consolidation_enabled: false,
  conflict_threshold: null,
  created_at: "",
  updated_at: "",
  daily_user_cost_cap_usd: 2,
  budget_alert_inbox: "warnings",
};

it("saves a separate user cap and can disable its warning inbox", async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<NamespaceBudgetAlerts namespace={namespace} locked={false} save={save} />);
  const cap = screen.getByLabelText("Daily cap per user (USD)");
  fireEvent.change(cap, { target: { value: "3.5" } });
  await act(async () => fireEvent.blur(cap));
  expect(save).toHaveBeenCalledWith({ daily_user_cost_cap_usd: 3.5 });
  const inbox = screen.getByLabelText("Budget warning inbox");
  fireEvent.change(inbox, { target: { value: "" } });
  await act(async () => fireEvent.blur(inbox));
  expect(save).toHaveBeenCalledWith({ budget_alert_inbox: "" });
});

it("rejects partial or negative caps and cancels a draft with Escape", async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<NamespaceBudgetAlerts namespace={namespace} locked={false} save={save} />);
  const cap = screen.getByLabelText("Daily cap per user (USD)");
  for (const value of ["", "-1"]) {
    fireEvent.change(cap, { target: { value } });
    await act(async () => fireEvent.blur(cap));
  }
  fireEvent.change(cap, { target: { value: "4" } });
  fireEvent.keyDown(cap, { key: "Escape" });
  await act(async () => fireEvent.blur(cap));
  expect(cap).toHaveValue(2);
  expect(save).not.toHaveBeenCalled();
});

it("locks both policy fields when the namespace is not writable", () => {
  render(<NamespaceBudgetAlerts namespace={namespace} locked save={vi.fn()} />);
  expect(screen.getByLabelText("Daily cap per user (USD)")).toBeDisabled();
  expect(screen.getByLabelText("Budget warning inbox")).toBeDisabled();
});
