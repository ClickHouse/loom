import assert from "node:assert/strict";
import test from "node:test";

import { normalizeTranslatedTitle, rewriteSourceLinks } from "./prepare-content.mjs";

test("moves a generated translation title into Blume front matter", () => {
  const prepared = normalizeTranslatedTitle(
    "<!-- i18n: source=docs/getting-started/quickstart.md blob=abc model=test -->\n" +
      '> Machine translation.\n\n<a id="quickstart"></a>\n\n# Inicio rápido\n\nTexto.\n',
  );

  assert.equal(
    prepared,
    '---\ntitle: "Inicio rápido"\n---\n\n' +
      "<!-- i18n: source=docs/getting-started/quickstart.md blob=abc model=test -->\n" +
      '> Machine translation.\n\n<a id="quickstart"></a>\n\nTexto.\n',
  );
});

test("leaves English content unchanged", () => {
  const source = "# Quickstart\n\nText.\n";
  assert.equal(normalizeTranslatedTitle(source), source);
});

test("keeps translated repository links outside the docs site", () => {
  const source = "[README](../../README.md)\n";
  const expected = "[README](https://github.com/ClickHouse/loom/blob/main/README.md)\n";

  assert.equal(rewriteSourceLinks(source, "development/repository-guide.md"), expected);
  assert.equal(rewriteSourceLinks(source, "ja/development/repository-guide.md"), expected);
});
