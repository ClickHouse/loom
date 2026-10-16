import { render, screen, within } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { axeViolations } from "../../test/axe";
import { Chip } from "./Chip";
import { Kbd } from "./Kbd";
import { MetaList, MetaRow } from "./Meta";
import { Panel } from "./Panel";
import { SectionHeading } from "./SectionHeading";
import { TextClamp } from "./TextClamp";
import { Tip } from "./Tooltip";
import { must } from "../../test/must";

// The primitive set: each renders in isolation, so each is pinned here with its contract
// and an axe pass. Tooltips open on focus, which jsdom can do; contrast is a browser
// question.

describe("Panel", () => {
  it("renders a labelled aside with a header outside the scroll region", async () => {
    const { container } = render(
      <Panel as="aside" aria-label="Inspector" scroll header={<SectionHeading label="Selected" />}>
        <p>body</p>
      </Panel>,
    );
    const aside = screen.getByRole("complementary", { name: "Inspector" });
    expect(aside.className).toContain("overflow-hidden");
    const body = must(screen.getByText("body").parentElement, "panel body wrapper");
    expect(body.className).toContain("overflow-y-auto");
    expect(screen.getByRole("heading", { name: "Selected" })).toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);
  });
});

describe("SectionHeading", () => {
  it("is one heading with a count, a caption and a hint that opens as a tooltip", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <SectionHeading
        label="Memory types"
        count={3}
        caption="in namespace"
        hint="Counts are memories of each type in the namespace, not in this walk."
      />,
    );
    expect(screen.getByRole("heading", { level: 3, name: "Memory types" })).toBeInTheDocument();
    expect(screen.getByText("3")).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByText("3 memory types")).toHaveClass("sr-only");
    expect(screen.getByText("in namespace")).toBeInTheDocument();
    const about = screen.getByRole("button", { name: "About Memory types" });
    await user.tab();
    expect(about).toHaveFocus();
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Counts are memories of each type in the namespace");
    // Nothing in the row truncates.
    for (const el of Array.from(container.querySelectorAll("*"))) {
      expect(el.getAttribute("class") ?? "").not.toMatch(/\btruncate\b/);
    }
    expect(await axeViolations(container)).toEqual([]);
  });
});

describe("Chip", () => {
  it("is a pressed toggle with its count qualified for assistive technology", async () => {
    const onClick = vi.fn();
    const { container } = render(
      <Chip label="semantic" count={9518} countLabel="in namespace" selected onClick={onClick} />,
    );
    const chip = screen.getByRole("button", { name: "semantic, 9518 in namespace" });
    expect(chip).toHaveAttribute("aria-pressed", "true");
    expect(chip.className).toContain("min-h-6");
    await userEvent.click(chip);
    expect(onClick).toHaveBeenCalledOnce();
    expect(await axeViolations(container)).toEqual([]);
  });

  it("a removable chip has a separate Remove button and a static chip is not a button", async () => {
    const onRemove = vi.fn();
    const { container } = render(
      <>
        <Chip label="uses" onClick={() => undefined} onRemove={onRemove} />
        <Chip label="run-1" mono />
      </>,
    );
    await userEvent.click(screen.getByRole("button", { name: "Remove uses" }));
    expect(onRemove).toHaveBeenCalledOnce();
    expect(screen.getAllByRole("button")).toHaveLength(2);
    expect(screen.getByText("run-1").closest("button")).toBeNull();
    expect(await axeViolations(container)).toEqual([]);
  });
});

describe("MetaList", () => {
  it("is a definition list whose values wrap rather than truncate", async () => {
    const { container } = render(
      <MetaList aria-label="Node">
        <MetaRow label="degree">381 — co-mentions on the edge that reached it</MetaRow>
        <MetaRow label="held" mono>
          2026-01-01 – now
        </MetaRow>
        <MetaRow label="stale" tone="warn">
          a row behind this edge has no active source
        </MetaRow>
      </MetaList>,
    );
    const dl = must(container.querySelector("dl"), "meta list <dl>");
    expect(within(dl).getAllByRole("term")).toHaveLength(3);
    const defs = within(dl).getAllByRole("definition");
    expect(defs).toHaveLength(3);
    expect(defs[0].className).toContain("break-words");
    expect(defs[0].className).not.toContain("truncate");
    expect(defs[1].className).toContain("font-mono");
    expect(await axeViolations(container)).toEqual([]);
  });
});

describe("TextClamp", () => {
  const long = "Fix LOGICAL_ERROR in RIGHT/FULL join with a constant condition ".repeat(6).trim();

  it("tooltip reach: the clamped text is focusable and the full text opens on focus", async () => {
    const user = userEvent.setup();
    const { container } = render(<TextClamp text={long} lines={1} max={80} reach="tooltip" />);
    const span = screen.getByText((t) => t.endsWith("…"));
    expect(span).toHaveAttribute("tabindex", "0");
    expect(must(span.textContent, "clamped text").length).toBeLessThanOrEqual(81);
    await user.tab();
    expect(span).toHaveFocus();
    expect(await screen.findByRole("tooltip")).toHaveTextContent(long);
    expect(await axeViolations(container)).toEqual([]);
  });

  it("expand reach: Show more reveals the whole text and says so", async () => {
    const user = userEvent.setup();
    const { container } = render(<TextClamp text={long} lines={2} max={80} reach="expand" />);
    const more = screen.getByRole("button", { name: "Show more" });
    expect(more).toHaveAttribute("aria-expanded", "false");
    await user.click(more);
    expect(screen.getByText(long)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show less" })).toHaveAttribute("aria-expanded", "true");
    expect(await axeViolations(container)).toEqual([]);
  });

  it("text that fits carries no tab stop and no button", () => {
    render(<TextClamp text="short" lines={1} max={80} reach="tooltip" />);
    expect(screen.getByText("short")).not.toHaveAttribute("tabindex");
    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("Kbd and Tip", () => {
  it("a key hint is a kbd element and a Tip opens from its focusable child", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <p>
        <Kbd>↵</Kbd> select ·{" "}
        <Tip content="Walk from this entity">
          <button type="button">Walk</button>
        </Tip>
      </p>,
    );
    expect(container.querySelector("kbd")).toHaveTextContent("↵");
    await user.tab();
    expect(screen.getByRole("button", { name: "Walk" })).toHaveFocus();
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Walk from this entity");
    expect(await axeViolations(container)).toEqual([]);
  });
});
