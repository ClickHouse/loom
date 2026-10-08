import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { axeViolations } from "../../../test/axe";

// Patterns mode opened on a namespace without a QA binding lists the tracker sources the
// caller reads; one is opened without a click; none is a stated result with the way to
// join.

const listTrackerSources = vi.fn();
const setSavedNamespace = vi.fn();
vi.mock("../../../api", () => ({
  listTrackerSources: (...a: unknown[]) => listTrackerSources(...a),
  setSavedNamespace: (...a: unknown[]) => setSavedNamespace(...a),
}));
vi.mock("../../../lib/namespaceRole", () => ({ resetNamespaceRoleCache: vi.fn() }));

const setIdentity = vi.fn();
vi.mock("../../../lib/state", () => ({
  useStore: (
    sel: (s: {
      identity: { org: string; namespace: string; agent: string };
      setIdentity: typeof setIdentity;
    }) => unknown,
  ) => sel({ identity: { org: "acme", namespace: "personal-me", agent: "ui" }, setIdentity }),
}));

import { SourcePicker, readableTrackerNamespaces } from "./SourcePicker";
import type { TrackerSource, TrackerSourceNamespace } from "../../../lib/trackerSources";

function ns(
  namespace: string,
  member = true,
  classification: TrackerSourceNamespace["classification"] = "standard",
): TrackerSourceNamespace {
  return {
    namespace,
    kind: "tracker",
    visibility: "org",
    repos: ["acme/app"],
    classification,
    join_policy: "open",
    registered: true,
    member_role: member ? "viewer" : "",
    pending_role: "",
  };
}
function source(name: string, namespaces: TrackerSourceNamespace[]): TrackerSource {
  return {
    name,
    host: "h",
    port: 8443,
    database: "qa",
    configured: true,
    origin: "config",
    contract: { ok: true, checked_at: "", summary: "", optional_present: [], features: {} },
    breaker: { state: "CLOSED", consecutive_failures: 0 },
    freshness: {},
    namespaces,
  };
}

beforeEach(() => {
  listTrackerSources.mockReset();
  setIdentity.mockReset();
  setSavedNamespace.mockReset();
});

describe("readableTrackerNamespaces", () => {
  it("keeps only the namespaces the caller is a member of, in the Repositories order", () => {
    const out = readableTrackerNamespaces([
      source("tracker-b", [ns("tracker-b-main", false)]),
      source("tracker-a", [ns("tracker-a-main"), ns("tracker-a-priv", true, "restricted")]),
    ]);
    expect(out.map((r) => r.ns.namespace)).toEqual(["tracker-a-main", "tracker-a-priv"]);
  });
});

describe("SourcePicker", () => {
  it("opens the one readable tracker namespace without a click", async () => {
    listTrackerSources.mockResolvedValue({ sources: [source("tracker-a", [ns("tracker-a-main")])] });
    render(<SourcePicker org="acme" current="personal-me" />);
    await waitFor(() =>
      expect(setIdentity).toHaveBeenCalledWith({ org: "acme", namespace: "tracker-a-main", agent: "ui" }),
    );
    expect(setSavedNamespace).toHaveBeenCalledWith("tracker-a-main");
  });

  it("lists several readable namespaces and opens the chosen one; passes axe", async () => {
    listTrackerSources.mockResolvedValue({
      sources: [source("tracker-a", [ns("tracker-a-main"), ns("tracker-a-priv", true, "restricted")])],
    });
    const { container } = render(<SourcePicker org="acme" current="personal-me" />);
    const list = await screen.findByRole("list", { name: "tracker sources you read" });
    expect(list.querySelectorAll("li")).toHaveLength(2);
    expect(setIdentity).not.toHaveBeenCalled();
    await userEvent.click(screen.getAllByRole("button", { name: "Open" })[1]);
    expect(setIdentity).toHaveBeenCalledWith({ org: "acme", namespace: "tracker-a-priv", agent: "ui" });
    expect(await axeViolations(container)).toEqual([]);
  });

  it("states the result and where to join when the caller reads none", async () => {
    listTrackerSources.mockResolvedValue({ sources: [source("tracker-a", [ns("tracker-a-main", false)])] });
    render(<SourcePicker org="acme" current="personal-me" />);
    expect(await screen.findByText(/read no tracker namespace yet/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Repositories" })).toHaveAttribute("href", "/me/repositories");
    expect(setIdentity).not.toHaveBeenCalled();
  });
});
