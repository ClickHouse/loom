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

test("sets the search title without changing the translated heading or body", () => {
  const stamp = "<!-- i18n: source=docs/use-cases/agent-memory.md blob=abc model=test -->";
  const prepared = normalizeTranslatedTitle(`${stamp}\n\n# Memória de longo prazo e compartilhada\n\nTexto.\n`, {
    title: "Memória compartilhada para agentes de código",
  });
  assert.equal(
    prepared,
    '---\ntitle: "Memória de longo prazo e compartilhada"\n' +
      'seo:\n  title: "Memória compartilhada para agentes de código"\n---\n\n' +
      `${stamp}\n\nTexto.\n`,
  );
});

test("sets a search description on a translated docs homepage", () => {
  const stamp = "<!-- i18n: source=docs/index.md blob=abc model=test -->";
  const prepared = normalizeTranslatedTitle(`${stamp}\n\n# ClickHouse Loom 문서\n\n문서 본문.\n`, {
    description: "AI 에이전트를 위한 공유 메모리와 코드 인덱스.",
  });

  assert.equal(
    prepared,
    '---\ntitle: "ClickHouse Loom 문서"\n' +
      'seo:\n  description: "AI 에이전트를 위한 공유 메모리와 코드 인덱스."\n---\n\n' +
      `${stamp}\n\n문서 본문.\n`,
  );
});

test("keeps translated repository links outside the docs site", () => {
  const source = "[README](../../README.md)\n";
  const expected = "[README](https://github.com/ClickHouse/loom/blob/main/README.md)\n";

  assert.equal(rewriteSourceLinks(source, "development/repository-guide.md"), expected);
  assert.equal(rewriteSourceLinks(source, "ja/development/repository-guide.md"), expected);
});
