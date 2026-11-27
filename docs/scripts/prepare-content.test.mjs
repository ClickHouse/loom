import assert from "node:assert/strict";
import test from "node:test";
import { load } from "js-yaml";

import { withContentMetadata } from "./content-metadata.mjs";
import {
  normalizeTranslatedTitle,
  prepareLocalizedLandingDemo,
  prepareTranslatedMarkup,
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
    const prepared = withContentMetadata(`---\n${header}\n---\n${body}`, { date });
    const [, yaml, content] = prepared.split(/^---\n/m);
    assert.deepEqual(load(yaml), { title: "Page", lastModified: date });
    assert.equal(content, body);
  }
  assert.deepEqual(load(withContentMetadata(body, { date }).split(/^---\n/m)[1]), { lastModified: date });
  for (const key of ["lastModified", '"lastModified"', "'lastModified'"]) {
    const pinned = `---\n${key}: "2025-01-03"\n---\n${body}`;
    assert.equal(withContentMetadata(pinned, { date }), pinned);
  }
  assert.equal(withContentMetadata(body), body);
});

test("uses translated prose without a translation notice, markup, or link URL", () => {
  const source = [
    "# Memória para agentes",
    "> Esta página foi traduzida automaticamente. Consulte a documentação original.",
    "~~~bash\n./install.sh\n~~~",
    "Consulte [as permissões](permissions.md) antes de compartilhar uma memória com outros agentes.",
  ].join("\n\n");
  const prepared = withContentMetadata(source, { locale: "pt-BR" });
  const [, yaml, body] = prepared.split(/^---\n/m);
  assert.equal(load(yaml).description, "Consulte as permissões antes de compartilhar uma memória com outros agentes.");
  assert.equal(body, `\n${source}`);
});

test("keeps editorial descriptions and adds a fallback beside an editorial search title", () => {
  const body = "A user can inspect the source and history of a memory through its dashboard.";
  for (const description of ["description: Editorial summary", "seo: {description: Editorial summary}"]) {
    const source = `---\n${description}\n---\n\n${body}`;
    assert.equal(withContentMetadata(source), source);
  }
  const source = `---\nseo:\n  title: A specific title\n---\n\n${body}`;
  const yaml = withContentMetadata(source).split(/^---\n/m)[1];
  assert.deepEqual(load(yaml), { seo: { title: "A specific title" }, description: body });
});

test("keeps a complete lead sentence when the rest of a paragraph does not fit", () => {
  const lead = "自律型エージェントは保存したチェックポイントを読み取り、中断した処理を再開できます。";
  const source = lead + "次の項目を処理する前に前回の状態を確認します".repeat(10) + "。";
  assert.equal(load(withContentMetadata(source, { locale: "ja" }).split(/^---\n/m)[1]).description, lead);
});

test("trims a long sentence at a word boundary", () => {
  const source = "Start the job with the same identity and use the saved ".repeat(6).trimEnd() + ".";
  const description = load(withContentMetadata(source).split(/^---\n/m)[1]).description;
  assert(description.length <= 160 && description.endsWith("…"));
  assert(source.startsWith(description.slice(0, -1) + " "));
});

test("keeps a compact Chinese introduction ahead of shared reference text", () => {
  const source =
    "每个 `org.*` 操作接受和返回的确切字段。\n\n每个操作都是通过 HTTP 调用，身份字段在传输层能够提供时会按令牌自动填充。";
  const yaml = withContentMetadata(source, { locale: "zh-CN" }).split(/^---\n/m)[1];
  assert.equal(load(yaml).description, "每个 org.* 操作接受和返回的确切字段。");
});

test("omits introductory fragments whose lists or tables would be left out", () => {
  const prose = "The client reads memory over MCP and can retrieve lessons with their source.";
  for (const source of [
    `The client configuration has three parts:\n\n| Field | Meaning |\n| --- | --- |\n| URL | Server |\n\n${prose}`,
    `${prose} The client configuration has three parts:\n\n- Server URL\n- Token\n- Toolset`,
  ]) {
    const yaml = withContentMetadata(source).split(/^---\n/m)[1];
    assert.equal(load(yaml).description, prose);
  }
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

test("sets the search title and description on a translated docs homepage", () => {
  const stamp = "<!-- i18n: source=docs/index.md blob=abc model=test -->";
  const prepared = normalizeTranslatedTitle(`${stamp}\n\n# ClickHouse Loom 문서\n\n문서 본문.\n`, {
    title: "에이전트 메모리 및 코드 검색 문서",
    description: "AI 에이전트를 위한 공유 메모리와 코드 인덱스.",
  });

  assert.equal(
    prepared,
    '---\ntitle: "ClickHouse Loom 문서"\n' +
      'seo:\n  title: "에이전트 메모리 및 코드 검색 문서"\n' +
      '  description: "AI 에이전트를 위한 공유 메모리와 코드 인덱스."\n---\n\n' +
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
  assert.equal(prepareTranslatedMarkup(original, sources), expected);
});

test("preserves repository source links, images and examples", () => {
  const github = "https://github.com/ClickHouse/loom/blob/main/";
  const original = [
    `[README](${github}README.md)`,
    "[External docs](https://example.com/ClickHouse/loom/blob/main/docs/index.md)",
    `[Missing page](${github}docs/missing.md)`,
    `[Source lines](${github}docs/index.md#L3-L7)`,
    `[Plain source](${github}docs/index.md?plain=1)`,
    `![Image](${github}docs/index.md)`,
    `\`[Doc](${github}docs/index.md)\``,
    `~~~md\n[Doc](${github}docs/index.md)\n~~~`,
  ].join("\n\n");
  assert.equal(prepareTranslatedMarkup(original, new Set(["index.md"])), original);
});

test("keeps a translated multiline code span on one line for link validation", () => {
  const source = 'Some HTML syntax `<p\nalign="center"\nclass="intro">` and `# <img src="logo.svg">`.\n\nParagraph.';
  const expected = 'Some HTML syntax `<p align="center" class="intro">` and `# <img src="logo.svg">`.\n\nParagraph.';
  assert.equal(prepareTranslatedMarkup(source, new Set()), expected);
});
