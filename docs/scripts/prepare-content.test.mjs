import assert from "node:assert/strict";
import test from "node:test";
import { load } from "js-yaml";

import { withContentDate } from "./content-dates.mjs";
import {
  normalizeTranslatedTitle,
  prepareLocalizedLandingDemo,
  rewritePublishedDocLinks,
  rewriteSourceLinks,
} from "./prepare-content.mjs";

test("defers the localized landing demo and uses the published media file", () => {
  const file = "docs/assets/screenshots/clip-assistant-answer.webm";
  const source = `<video src="https://github.com/ClickHouse/loom/raw/main/${file}" controls autoplay loop muted><a href="https://github.com/ClickHouse/loom/blob/main/${file}">確認済みの出典付きの回答</a></video>`;
  const prepared = prepareLocalizedLandingDemo(source);
  const media = "../assets/screenshots/clip-assistant-answer.webm";
  assert.equal(
    prepared,
    `<details>\n<summary>確認済みの出典付きの回答</summary>\n<video src="${media}" controls preload="none" muted><a href="${media}">確認済みの出典付きの回答</a></video>\n</details>`,
  );
  assert.equal(prepareLocalizedLandingDemo(prepared), prepared);

  const refreshed = `<details>\n<summary>デモを見る</summary>\n${source.replace("autoplay loop", 'preload="none"')}\n</details>`;
  assert.equal(
    prepareLocalizedLandingDemo(refreshed),
    prepared.replace("<summary>確認済みの出典付きの回答</summary>", "<summary>デモを見る</summary>"),
  );
});

test("adds the source date without changing page content or overriding an explicit date", () => {
  const date = "2025-01-02T03:04:05.000Z";
  const body = "# Page\n\nContent.\n";
  for (const header of ["title: Page", "{title: Page}"]) {
    const prepared = withContentDate(`---\n${header}\n---\n${body}`, date);
    const [, yaml, content] = prepared.split(/^---\n/m);
    assert.deepEqual(load(yaml), { title: "Page", lastModified: date });
    assert.equal(content, body);
  }
  assert.deepEqual(load(withContentDate(body, date).split(/^---\n/m)[1]), { lastModified: date });
  for (const key of ["lastModified", '"lastModified"', "'lastModified'"]) {
    const pinned = `---\n${key}: "2025-01-03"\n---\n${body}`;
    assert.equal(withContentDate(pinned, date), pinned);
  }
  assert.equal(withContentDate(body, undefined), body);
});

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

test("sets localized search metadata without changing the translated heading or body", () => {
  const stamp = "<!-- i18n: source=docs/use-cases/agent-memory.md blob=abc model=test -->";
  const prepared = normalizeTranslatedTitle(`${stamp}\n\n# Memória de longo prazo e compartilhada\n\nTexto.\n`, {
    title: "Memória compartilhada para agentes de código",
    description: "Compartilhe decisões e aprendizados entre agentes de programação.",
  });
  assert.equal(
    prepared,
    '---\ntitle: "Memória de longo prazo e compartilhada"\n' +
      'seo:\n  title: "Memória compartilhada para agentes de código"\n' +
      '  description: "Compartilhe decisões e aprendizados entre agentes de programação."\n---\n\n' +
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

test("routes translated GitHub docs fallbacks to the published English pages", () => {
  const github = "https://github.com/ClickHouse/loom/blob/main/docs/";
  const docs = "https://loom.clickhouse.com/docs";
  const original =
    `> [English original](${github}index.md)\n\n` +
    `[Setup [details]](${github}getting-started/index.md#setup "Open the setup guide")\n\n` +
    `<${github}getting-started/quickstart.md#configuration>\n\n` +
    "[Japanese setup](../getting-started/index.md#setup)\n";
  const expected =
    `> [English original](${docs})\n\n` +
    `[Setup [details]](${docs}/getting-started#setup "Open the setup guide")\n\n` +
    `<${docs}/getting-started/quickstart#configuration>\n\n` +
    "[Japanese setup](../getting-started/index.md#setup)\n";
  const sources = new Set(["index.md", "getting-started/index.md", "getting-started/quickstart.md"]);
  assert.equal(rewritePublishedDocLinks(original, sources), expected);
});

test("preserves repository source links, images and examples", () => {
  const github = "https://github.com/ClickHouse/loom/blob/main/";
  const original = [
    `[README](${github}README.md)`,
    `[Missing page](${github}docs/missing.md)`,
    `[Source lines](${github}docs/index.md#L3-L7)`,
    `[Plain source](${github}docs/index.md?plain=1)`,
    `![Image](${github}docs/index.md)`,
    `\`[Doc](${github}docs/index.md)\``,
    `~~~md\n[Doc](${github}docs/index.md)\n~~~`,
  ].join("\n\n");
  assert.equal(rewritePublishedDocLinks(original, new Set(["index.md"])), original);
});
