import { act, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EmptyState } from "../components/EmptyState";
import { ScopeBadge } from "../components/ScopeBadge";
import { ToastViewport, toast } from "../components/Toast";
import { axeViolations } from "./axe";

// axe-core over components that render in isolation. Views need identity, the store and
// the API; e2e/a11y.spec.ts runs axe on every route in the browser.

describe("axe: no violations", () => {
  it("EmptyState with a description and an action", async () => {
    const { container } = render(
      <EmptyState
        icon="share-network"
        title="No entities yet"
        description="The graph fills as memories with entities are written."
        action={{ label: "Write a memory", onClick: () => undefined }}
      />,
    );
    expect(screen.getByRole("heading", { name: "No entities yet" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Write a memory" })).toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);
  });

  it("ScopeBadge in each of its three states", async () => {
    const { container } = render(
      <>
        <ScopeBadge kind="namespace" value="team-a" />
        <ScopeBadge kind="namespace" value="" />
        <ScopeBadge kind="loom-wide" />
      </>,
    );
    expect(await axeViolations(container)).toEqual([]);
  });

  it("ToastViewport with a success toast and an error toast showing", async () => {
    const { container } = render(<ToastViewport />);
    act(() => {
      toast("Saved the pick", "ok");
      toast("Load failed", "error");
    });
    // The viewport is the polite live region; an error toast is an alert; every toast has
    // a labelled dismiss button.
    expect(container.querySelector("[aria-live='polite']")).toHaveTextContent("Saved the pick");
    expect(screen.getByRole("alert")).toHaveTextContent("Load failed");
    expect(screen.getAllByRole("button", { name: "Dismiss notification" })).toHaveLength(2);
    expect(await axeViolations(container)).toEqual([]);
  });
});
