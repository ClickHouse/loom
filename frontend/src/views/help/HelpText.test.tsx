import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { inline } from "../assistant/AnswerText";
import { HelpText, helpHref } from "./HelpText";

const PAGE = `# Tokens and grants

Mint one with [the CLI](../reference/cli.md#mint) or read [the roles](#choose-roles).
An [external page](https://example.com/x) and ![a screenshot](../images/x.png) stay text,
and so does [a contributor page](../development/testing.md).

\`\`\`bash
# not a heading
\`\`\`

## Choose roles

<script>window.pwned = 1</script>
`;

const OUTLINE = [
  { section: "Tokens and grants", anchor: "tokens-and-grants", heading: "Tokens and grants" },
  { section: "Tokens and grants > Choose roles", anchor: "choose-roles", heading: "Choose roles" },
];

describe("HelpText", () => {
  it("draws headings with the server's anchors, and only links into the docs", () => {
    const { container } = render(<HelpText path="guides/tokens-and-grants.md" text={PAGE} outline={OUTLINE} />);
    // The title is the page header's: here only its anchor.
    expect(screen.queryByRole("heading", { name: "Tokens and grants" })).toBeNull();
    expect(container.querySelector("#tokens-and-grants")).not.toBeNull();
    expect(screen.getByRole("heading", { name: "Choose roles" })).toHaveAttribute("id", "choose-roles");
    expect(screen.queryByRole("heading", { name: "not a heading" })).toBeNull();
    const hrefs = screen.getAllByRole("link").map((a) => a.getAttribute("href"));
    expect(hrefs).toEqual([
      "/help?page=reference%2Fcli.md#mint",
      "/help?page=guides%2Ftokens-and-grants.md#choose-roles",
    ]);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container).toHaveTextContent("external page");
    expect(container).toHaveTextContent("<script>window.pwned = 1</script>");
  });

  it("gives each heading the server's anchor for the heading written the same way", () => {
    // A setext heading the page draws as text, and a heading whose anchor the server's rule
    // gives (link target and all): neither shifts the anchors after it.
    const text = "# Page\n\nSetext\n------\n\n## See [the roles](roles.md)\n\n## Same\n\n## Same\n";
    const outline = [
      { section: "Page", anchor: "page", heading: "Page" },
      { section: "Page > Setext", anchor: "setext", heading: "Setext" },
      { section: "Page > See the roles", anchor: "see-the-rolesrolesmd", heading: "See [the roles](roles.md)" },
      { section: "Page > Same", anchor: "same", heading: "Same" },
      { section: "Page > Same", anchor: "same-1", heading: "Same" },
    ];
    const { container } = render(<HelpText path="a.md" text={text} outline={outline} />);
    const ids = [...container.querySelectorAll("h2")].map((h) => h.id);
    expect(ids).toEqual(["see-the-rolesrolesmd", "same", "same-1"]);
    // A second `#` heading is drawn, as a section.
    const { container: two } = render(<HelpText path="a.md" text={"# One\n\n# Two\n"} outline={[]} />);
    expect(two.querySelector("h2")).toHaveTextContent("Two");
    // A setext title (drawn as text) leaves the first `##` heading drawn.
    const setext = [
      { section: "Title", anchor: "title", heading: "Title" },
      { section: "Title > Install", anchor: "install", heading: "Install" },
    ];
    const { container: under } = render(
      <HelpText path="a.md" text={"Title\n=====\n\n## Install\n\nSteps."} outline={setext} />,
    );
    expect(under.querySelector("h2")).toHaveTextContent("Install");
    expect(under.querySelector("h2")).toHaveAttribute("id", "install");
    // The first heading is the title whatever its level, as the server reads it.
    const { container: deep } = render(<HelpText path="a.md" text={"## Intro\n\n# Two\n"} outline={[]} />);
    expect([...deep.querySelectorAll("h2")].map((h) => h.textContent)).toEqual(["Two"]);
    // A heading the outline does not list gets no anchor.
    const { container: bare } = render(<HelpText path="a.md" text={"# T\n\n## Not listed"} outline={[]} />);
    expect(bare.querySelector("h2")?.id).toBe("");
  });

  it("reads links with code spans and emphasis as Markdown does", () => {
    const linksOf = (text: string) => {
      const { container } = render(<HelpText path="a.md" text={text} outline={[]} />);
      const links = [...container.querySelectorAll("a")];
      return { container, links: links.map((l) => [l.textContent, l.getAttribute("href")]) };
    };
    // A link inside a code span is code; a code span as a label is a link's label.
    let got = linksOf("Use `[x](b.md)` or [`memory.inspect`](d.md#e).");
    expect(got.container.querySelector("code")).toHaveTextContent("[x](b.md)");
    expect(got.links).toEqual([["memory.inspect", "/help?page=d.md#e"]]);
    // A `[` inside code hides no link after it.
    expect(linksOf("Type `[` to open [the guide](b.md).").links).toEqual([["the guide", "/help?page=b.md"]]);
    // A code span that opens in a label and closes past it is code: no link.
    got = linksOf("[not a `link](/foo`)");
    expect(got.links).toEqual([]);
    expect(got.container.querySelector("code")).toHaveTextContent("link](/foo");
    // Emphasis around a link, and around a code span, is emphasis.
    got = linksOf("**[Quickstart](quickstart.md)** and **with `care`**");
    expect(got.links).toEqual([["Quickstart", "/help?page=quickstart.md"]]);
    expect(got.container.querySelector("strong a")).not.toBeNull();
    expect(got.container.querySelectorAll("strong")[1]).toHaveTextContent("with care");
    // A label with brackets is a link; an empty one is text.
    expect(linksOf("[a [b] c](d.md) and [](e.md)").links).toEqual([["a [b] c", "/help?page=d.md"]]);
    // Links do not nest: the inner link wins and the outer brackets are text. A link may
    // hold an image, and an image's alt shows a link in it as its label.
    got = linksOf("[a [b](c.md) d](e.md)");
    expect(got.links).toEqual([["b", "/help?page=c.md"]]);
    expect(got.container).toHaveTextContent("[a b d](e.md)");
    expect(linksOf('[a [b](c.md) d](e.md "Guide")').container).toHaveTextContent('[a b d](e.md "Guide")');
    expect(linksOf("[![badge](x.svg)](y.md)").links).toEqual([["badge", "/help?page=y.md"]]);
    got = linksOf("![a [b](c.md) d](x.svg)");
    expect(got.links).toEqual([]);
    expect(got.container).toHaveTextContent("a b d");
    // A backtick that closes nothing is a label's character.
    expect(linksOf("[a `b](c.md)").links).toEqual([["a `b", "/help?page=c.md"]]);
  });

  it("draws no link in an answer", () => {
    const { container } = render(<>{inline("[x](b.md) **[y](c.md)**")}</>);
    expect(container.querySelector("a")).toBeNull();
    expect(container).toHaveTextContent("[x](b.md)");
  });

  it("resolves a relative page link, and refuses one outside the docs or not a page", () => {
    expect(helpHref("../concepts/memory.md", "features/memory/writing.md")).toBe(
      "/help?page=features%2Fconcepts%2Fmemory.md",
    );
    expect(helpHref("../../concepts/memory.md", "features/memory/writing.md")).toBe("/help?page=concepts%2Fmemory.md");
    expect(helpHref("../../../README.md", "features/memory/writing.md")).toBeNull();
    expect(helpHref("javascript:alert(1)", "index.md")).toBeNull();
    expect(helpHref("/etc/passwd", "index.md")).toBeNull();
    expect(helpHref("../integrations/x.yaml", "guides/a.md")).toBeNull();
  });
});
