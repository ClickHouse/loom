import ts from "typescript";
import { describe, expect, it } from "vitest";
import { escapeRegExp, opNames } from "../test/opNames";

// Text the dashboard renders names what the user sees, never an operation or an API path, on
// every page, operator pages included (docs/development/dashboard-design.md, "Rules a change
// is checked against"). The test parses App.tsx and every view, component, flow and layout
// file, `.ts` and `.tsx` (both as TSX), with the TypeScript parser; test files are left out.
//
// What it reads:
// - JSX text, in elements and fragments;
// - the literal text of every JSX attribute but those in `NOT_PROSE`, any `data-*`, and
//   `value` and `name` on a `WIRING` element;
// - inside those: both arms of a ?:, the right side of `||`, `&&` and `??`, both sides of a
//   `+`, a template's text and its `${…}` parts, and object and array literals, whose keys
//   follow the same deny-list (`{ label: "…" }` is read; `{ id: … }`, `{ value: … }` and
//   `{ name: … }` are not, nor is a shorthand property such as `{ onClick }`);
// - the first argument of a toast and of `committedAnyway`, and the fields a
//   `confirmAction({...})` shows.
//
// What it does not read, so review does:
// - text built in a variable, in `lib/*.ts`, or returned by a function, and an error's
//   message (`new Error(...)`);
// - text the server sends (a refusal's reason, a tier's fix), which the server words;
// - text inside <code>, <pre>, <Mono> or a `font-mono` element (a class that is mono
//   only under a condition counts as mono), and a mono field's placeholder; the element's own
//   props, and JSX inside them, are read.

const SOURCES = import.meta.glob<string>(
  [
    "../App.tsx",
    "../views/**/*.{ts,tsx}",
    "../components/**/*.{ts,tsx}",
    "../flows/**/*.{ts,tsx}",
    "../layout/**/*.{ts,tsx}",
    "!../**/*.test.{ts,tsx}",
  ],
  { query: "?raw", import: "default", eager: true },
);

/** Attributes that carry no prose: styling, wiring, routing, ids and enum-valued options.
 *  Every other attribute's literal text is read. */
const NOT_PROSE = new Set([
  // styling and enum options
  "className",
  "class",
  "style",
  "size",
  "state",
  "type",
  "htmlType",
  "variant",
  "width",
  "icon",
  "iconLeft",
  "iconRight",
  // identity and wiring
  "key",
  "id",
  "role",
  "htmlFor",
  "aria-controls",
  "aria-labelledby",
  "aria-describedby",
  // names a reader copies: `ExactNames` sets each one in <code>
  "refs",
  // where a link goes
  "href",
  "src",
  "to",
]);

/** Elements whose `value` and `name` are wiring: a tab's or an option's key, an icon's name,
 *  a form control's field. Elsewhere `value` and `name` may be shown (a `Stat`'s figure, a
 *  `RowMenu`'s "Actions for <name>"), so they are read. */
const WIRING = new Set([
  "Tabs.Content",
  "Tabs.Trigger",
  "TabTrigger",
  "Select.Item",
  "Icon",
  "input",
  "option",
  "select",
]);

/** Whether an attribute is read: not in `NOT_PROSE`, not a `data-*`, not a wiring element's
 *  `value` or `name`, and not a mono field's placeholder, which shows what the person types. */
function isProse(a: ts.JsxAttribute): boolean {
  const name = a.name.getText();
  if (NOT_PROSE.has(name) || name.startsWith("data-")) return false;
  if ((name === "value" || name === "name") && WIRING.has(a.parent.parent.tagName.getText())) return false;
  return !(name === "placeholder" && isCodeElement(a.parent.parent));
}

/** The fields of a `confirmAction({...})` that the dialog shows. */
const CONFIRM_FIELDS = new Set(["title", "message", "confirmLabel", "cancelLabel"]);

const TOASTS = new Set(["toast", "toastError", "toastErrorLine", "toastCommitted", "toastWithId", "committedAnyway"]);

const CODE_TAGS = new Set(["code", "pre", "Mono"]);

function isCodeElement(n: ts.JsxOpeningLikeElement): boolean {
  if (CODE_TAGS.has(n.tagName.getText())) return true;
  return n.attributes.properties.some(
    (a) =>
      ts.isJsxAttribute(a) &&
      a.name.getText() === "className" &&
      a.initializer !== undefined &&
      /\bfont-mono\b/.test(a.initializer.getText()),
  );
}

/** Whether a code element's children hold the node. An attribute, and JSX inside one, is not
 *  held by the element it is set on. */
function insideCode(node: ts.Node): boolean {
  let own: ts.Node | undefined;
  for (let p: ts.Node | undefined = node; p; p = p.parent) {
    if (ts.isJsxAttribute(p)) own = p.parent.parent;
    else if (ts.isJsxElement(p) && p !== node && p.openingElement !== own && isCodeElement(p.openingElement))
      return true;
  }
  return false;
}

/** The literal text an expression renders: strings, template text and its `${…}` parts,
 *  both arms of a ?:, the right side of `||`, `&&` and `??`, and both sides of a `+`. */
function literalText(e: ts.Expression | undefined): string[] {
  if (!e) return [];
  if (ts.isParenthesizedExpression(e)) return literalText(e.expression);
  if (ts.isStringLiteral(e) || ts.isNoSubstitutionTemplateLiteral(e)) return [e.text];
  if (ts.isTemplateExpression(e))
    return [
      e.head.text,
      ...e.templateSpans.map((s) => s.literal.text),
      ...e.templateSpans.flatMap((s) => literalText(s.expression)),
    ];
  if (ts.isConditionalExpression(e)) return [...literalText(e.whenTrue), ...literalText(e.whenFalse)];
  if (ts.isBinaryExpression(e)) {
    const op = e.operatorToken.kind;
    if (op === ts.SyntaxKind.PlusToken) return [...literalText(e.left), ...literalText(e.right)];
    if (
      op === ts.SyntaxKind.BarBarToken ||
      op === ts.SyntaxKind.AmpersandAmpersandToken ||
      op === ts.SyntaxKind.QuestionQuestionToken
    )
      return literalText(e.right);
  }
  if (ts.isArrayLiteralExpression(e)) return e.elements.flatMap((x) => literalText(x));
  if (ts.isObjectLiteralExpression(e))
    return e.properties.flatMap((prop) =>
      ts.isPropertyAssignment(prop) && isProseKey(prop.name.getText()) ? literalText(prop.initializer) : [],
    );
  return [];
}

/** An object literal's key is read as an attribute is: not one in `NOT_PROSE`, and not a
 *  `value` or `name`, which in an options list or a column spec is a key, not words. */
function isProseKey(key: string): boolean {
  return !NOT_PROSE.has(key) && key !== "value" && key !== "name" && !key.startsWith("data-");
}

/** Every piece of rendered text in a file, with its line. */
function renderedText(path: string, source: string): { line: number; text: string }[] {
  const file = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const out: { line: number; text: string }[] = [];
  const add = (node: ts.Node, texts: string[]) => {
    if (insideCode(node)) return;
    const line = file.getLineAndCharacterOfPosition(node.getStart()).line + 1;
    for (const text of texts) if (text.trim()) out.push({ line, text });
  };
  const visit = (node: ts.Node) => {
    if (ts.isJsxText(node)) add(node, [node.text]);
    else if (ts.isJsxExpression(node) && (ts.isJsxElement(node.parent) || ts.isJsxFragment(node.parent)))
      add(node, literalText(node.expression));
    else if (ts.isJsxAttribute(node) && node.initializer && isProse(node)) {
      const init = node.initializer;
      add(node, ts.isStringLiteral(init) ? [init.text] : ts.isJsxExpression(init) ? literalText(init.expression) : []);
    } else if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && TOASTS.has(node.expression.text))
      add(node, literalText(node.arguments[0]));
    else if (
      ts.isCallExpression(node) &&
      ts.isIdentifier(node.expression) &&
      node.expression.text === "confirmAction"
    ) {
      const arg = node.arguments[0];
      if (arg && ts.isObjectLiteralExpression(arg))
        for (const prop of arg.properties)
          if (ts.isPropertyAssignment(prop) && CONFIRM_FIELDS.has(prop.name.getText()))
            add(prop, literalText(prop.initializer));
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return out;
}

describe("rendered text", () => {
  it("names no operation and no API path", async () => {
    const { names, fromRegistry } = await opNames();
    if (fromRegistry) expect(names.size).toBeGreaterThan(300);
    const banned = new RegExp(`(?<![\\w.])(${[...names].map(escapeRegExp).join("|")})(?![\\w])|/v1/`, "g");
    const hits = Object.entries(SOURCES).flatMap(([path, source]) =>
      renderedText(path, source).flatMap(({ line, text }) =>
        [...text.matchAll(banned)].map((m) => `${path}:${line}: ${m[0]}`),
      ),
    );
    expect(Object.keys(SOURCES).length).toBeGreaterThan(100);
    expect(hits).toEqual([]);
  });

  it("reads the text it checks: JSX text, visible props, toasts and confirmations, but not code", () => {
    const sample = [
      'const a = <p title="Plain">Runs memory.search</p>;',
      'const b = <Alert text={x ? `uses code.map` : "no"} />;',
      'toast("Saved via memory.set");',
      "const c = <code>memory.get</code>;",
      'const d = <span className="font-mono">memory.list</span>;',
      'const e = <Field id="memory.pin" />;',
      'const f = <p>{ok && "Runs memory.search"}</p>;',
      'const g = <>{"via memory.ask"}</>;',
      'const h = <Alert text={msg ?? "call run.begin"} />;',
      'const i = <Alert text={"open " + name + " with memory.get"} />;',
      'confirmAction({ title: "Delete?", message: `runs memory.delete`, confirmLabel: "Delete", danger: true });',
      'const j = <Alert text={`ran ${x ? "via memory.get" : ""}`} />;',
      'const k = <Tip content="Runs code.search"><b /></Tip>;',
      'const l = <Unavailable reason="needs memory.set"><b /></Unavailable>;',
      'const m = <TextField className="font-mono" label="Runs memory.pin" placeholder="memory.delete" />;',
      'const n = <TextField className="font-mono" label="Uses memory.pin">memory.x</TextField>;',
      'const o = <span className="font-mono" title="via memory.ask">x</span>;',
      'const q = <ReplayFigure label="Cost" help="from cost.record_turns">x</ReplayFigure>;',
      'const r = <Table noDataMessage="nothing in memory.list" />;',
      "const s = <Mono title={<b>via memory.ask</b>}>memory.x</Mono>;",
      'const u = <Button className="x" icon="memory.x" data-op="memory.x" type="memory.x" />;',
      'const v = <Stat label="Mode" value="via memory.ask" />;',
      'const w = <Select.Item value="memory.x">Memory</Select.Item>;',
      'const x = <EmptyState action={{ label: "Run memory.search", onClick: go, id: "memory.x" }} />;',
      'const y = <Table headers={[{ label: "code.ask spend", width: "80px" }]} />;',
      'const z = <ButtonGroup options={[{ value: "memory.x", label: "Memory" }]} />;',
      'committedAnyway("Saved via memory.set", e);',
      'confirmAction({ title: "Go?", cancelLabel: "Keep memory.pin" });',
      'const ex = <ExactNames refs={["memory.x"]} />;',
    ].join("\n");
    const texts = renderedText("sample.tsx", sample).map((t) => t.text.trim());
    expect(texts).toEqual([
      "Plain",
      "Runs memory.search",
      "uses code.map",
      "no",
      "Saved via memory.set",
      "Runs memory.search",
      "via memory.ask",
      "call run.begin",
      "open",
      "with memory.get",
      "Delete?",
      "runs memory.delete",
      "Delete",
      "ran",
      "via memory.get",
      "Runs code.search",
      "needs memory.set",
      "Runs memory.pin",
      "Uses memory.pin",
      "via memory.ask",
      "Cost",
      "from cost.record_turns",
      "x",
      "nothing in memory.list",
      "via memory.ask",
      "Mode",
      "via memory.ask",
      "Memory",
      "Run memory.search",
      "code.ask spend",
      "Memory",
      "Saved via memory.set",
      "Go?",
      "Keep memory.pin",
    ]);
  });
});
