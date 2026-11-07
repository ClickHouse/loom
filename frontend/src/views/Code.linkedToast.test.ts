/**
 * The confirmation after a tracker link: the server's warning (branches that read the links
 * too, trackers that do not cover the repository) is shown, and long enough to read.
 */
import { describe, expect, it } from "vitest";
import { linkedToast } from "./Code";

describe("linkedToast", () => {
  it("confirms a link with no warning as a short success", () => {
    expect(linkedToast("acme/widgets", { tracker_namespaces: ["tracker-w"], warning: "" })).toEqual([
      "acme/widgets reads tracker-w",
      "ok",
      {},
    ]);
  });

  it("carries the server's warning and stays on screen longer", () => {
    const [msg, kind, opts] = linkedToast("acme/widgets", {
      tracker_namespaces: ["tracker-w"],
      warning: "the readers of code-widgets-26-8 read these links too",
    });
    expect(msg).toBe("acme/widgets reads tracker-w: the readers of code-widgets-26-8 read these links too");
    expect(kind).toBe("info");
    expect(opts.durationMs).toBeGreaterThan(3500);
  });

  it("says unlinked for an empty list", () => {
    expect(linkedToast("acme/widgets", { tracker_namespaces: [], warning: "" })[0]).toBe("acme/widgets unlinked");
  });
});
