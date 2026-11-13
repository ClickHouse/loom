import { cleanup, render, screen } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { MyNamespaceEntry } from "../api";
import { useStore } from "../lib/state";
import { axeViolations } from "../test/axe";
import { NamespaceNoAccess } from "./NamespaceNoAccess";

// A link to a namespace the account cannot read opens this page in place of any view: it
// names the namespace and why, and Continue, the picker or Namespaces in your org leave it.

const team = { org: "dev", namespace: "team", agent: "loom-ui" };
const pending: MyNamespaceEntry = {
  namespace: "group-b-shared",
  role: "writer",
  status: "pending",
  pending_role: null,
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
};

describe("NamespaceNoAccess", () => {
  beforeEach(() => {
    history.replaceState(null, "", "/memories?namespace=group-b-shared&memory=k");
    useStore.setState({
      identity: team,
      deniedNamespace: "group-b-shared",
      myNamespaces: [],
      view: "memories",
      namespacePickerOpen: false,
    });
  });
  afterEach(() => cleanup());

  it("names the namespace and the namespace the page is on, and passes axe", async () => {
    const { container } = render(<NamespaceNoAccess namespace="group-b-shared" />);
    expect(screen.getByRole("heading", { level: 1, name: "No access to group-b-shared" })).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 2, name: "Your account cannot read this namespace" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Until then you can keep working in team\.$/)).toBeInTheDocument();
    expect(document.title).toBe("No access to group-b-shared · Loom");
    expect(await axeViolations(container)).toEqual([]);
  });

  it("Continue leaves it for the namespace under it, in place, and drops what the link named", async () => {
    render(<NamespaceNoAccess namespace="group-b-shared" />);
    const before = history.length;
    const proceed = screen.getByRole("button", { name: "Continue in team" });
    // The label stays one word; the name says where it continues.
    expect(proceed).toHaveTextContent(/^Continue$/);
    await userEvent.click(proceed);
    expect(useStore.getState()).toMatchObject({ deniedNamespace: null, identity: team, view: "memories" });
    expect(location.pathname + location.search).toBe("/memories?namespace=team");
    expect(history.length).toBe(before);
  });

  it("when the membership read failed, says the access could not be checked, never that it is refused", async () => {
    useStore.setState({ myNamespaces: null });
    const { container } = render(<NamespaceNoAccess namespace="group-b-shared" />);
    expect(
      screen.getByRole("heading", { level: 1, name: "Couldn't check your access to group-b-shared" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/cannot read|No access/)).toBeNull();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue in team" })).toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);
  });

  it("opens the namespace picker from its link", async () => {
    render(<NamespaceNoAccess namespace="group-b-shared" />);
    await userEvent.click(screen.getByRole("button", { name: "namespace picker" }));
    expect(useStore.getState().namespacePickerOpen).toBe(true);
  });

  it("says when a request to join it is waiting", () => {
    useStore.setState({ myNamespaces: [pending] });
    render(<NamespaceNoAccess namespace="group-b-shared" />);
    expect(
      screen.getByRole("heading", { level: 2, name: "Your request to join is waiting for approval" }),
    ).toBeInTheDocument();
  });

  it("with no namespace to fall back on, offers Namespaces in your org instead of Continue", async () => {
    useStore.setState({ identity: { ...team, namespace: "" } });
    render(<NamespaceNoAccess namespace="group-b-shared" />);
    expect(screen.queryByRole("button", { name: /^Continue/ })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Find public namespaces to join" }));
    expect(useStore.getState()).toMatchObject({ view: "browse", deniedNamespace: null });
  });
});
