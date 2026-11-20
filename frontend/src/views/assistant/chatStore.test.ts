import { describe, expect, it } from "vitest";
import { endAnnouncement } from "./chatStore";

describe("endAnnouncement", () => {
  it("says the end with the label, or alone with none", () => {
    expect(endAnnouncement({ label: "Couldn't confirm: it gives no sources", allFound: false, saidDone: false })).toBe(
      "The answer is complete. Couldn't confirm: it gives no sources.",
    );
    expect(endAnnouncement({ label: "", allFound: false, saidDone: false })).toBe("The answer is complete.");
  });

  it("says a fully confirmed label with the end when the end was not said yet", () => {
    expect(endAnnouncement({ label: "Checked against what Loom holds", allFound: true, saidDone: false })).toBe(
      "The answer is complete. Checked against what Loom holds.",
    );
  });

  it("after the end was said, says only a label not fully confirmed", () => {
    expect(endAnnouncement({ label: "Couldn't confirm: it gives no sources", allFound: false, saidDone: true })).toBe(
      "Couldn't confirm: it gives no sources.",
    );
    expect(endAnnouncement({ label: "Checked against what Loom holds", allFound: true, saidDone: true })).toBe("");
    expect(endAnnouncement({ label: "", allFound: false, saidDone: true })).toBe("");
  });
});
