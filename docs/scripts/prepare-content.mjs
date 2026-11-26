import { createHash } from "node:crypto";
import { cpSync, existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, normalize, relative, resolve, sep } from "node:path";
import { spawnSync } from "node:child_process";
import { escape } from "html-escaper";
import { fromMarkdown } from "mdast-util-from-markdown";
import sharp from "sharp";
import { readSiteDates } from "./content-dates.mjs";
import { withContentMetadata } from "./content-metadata.mjs";

const docs = dirname(fileURLToPath(import.meta.url));
const root = dirname(docs);
const repository = dirname(root);
const content = join(root, ".blume-content");
const docsMedia = join(root, "public", "docs-media");
const locales = ["es", "ja", "ko", "pt-BR", "zh-CN"];
const docsBlob = "https://github.com/ClickHouse/loom/blob/main/docs/";
const docsSite = "https://loom.clickhouse.com/docs";
const localizedSeo = {
  "architecture/storage-authority.md": {
    es: { title: "Almacenamiento en Postgres y ClickHouse" },
    "pt-BR": { title: "Armazenamento no Postgres e no ClickHouse" },
  },
  "benchmarks/adding-a-case.md": {
    "pt-BR": { title: "Adicionar casos, dados e suítes de benchmark" },
  },
  "development/adding-language-support.md": {
    es: { title: "Añadir lenguajes al índice de código" },
  },
  "features/tracker/overview.md": {
    es: { title: "Incidencias duplicadas y posibles regresiones" },
    "pt-BR": { title: "Issues duplicadas e candidatos a regressão" },
  },
  "use-cases/code-intelligence-by-language.md": {
    es: { title: "Inteligencia de código por lenguaje" },
    ja: { title: "言語別のコードインテリジェンス" },
    ko: { title: "언어별 코드 인텔리전스" },
    "pt-BR": { title: "Inteligência de código por linguagem" },
    "zh-CN": { title: "按语言查看代码智能支持" },
  },
  "features/integrations/cursor.md": {
    ja: {
      title: "Cursorの共有エージェントメモリをMCPで使う",
      description:
        "CursorをLoomにMCPで接続し、プロジェクトの判断や教訓をセッション間で共有します。ローカルとチーム向けの接続方法を紹介します。",
    },
    ko: {
      title: "Cursor에서 MCP로 공유 에이전트 메모리 사용",
      description:
        "Cursor를 MCP로 Loom에 연결해 프로젝트 결정과 교훈을 여러 세션에서 공유합니다. 로컬 연결과 팀 서버 설정을 확인할 수 있습니다.",
    },
    "zh-CN": {
      title: "在 Cursor 中通过 MCP 使用共享智能体记忆",
      description: "通过 MCP 将 Cursor 连接到 Loom，在不同会话中共享项目决策与经验，并为个人或团队配置连接。",
    },
  },
  "features/integrations/vscode.md": {
    es: {
      title: "Memoria compartida para GitHub Copilot en VS Code",
      description:
        "Conecta GitHub Copilot a Loom por MCP. Guarda decisiones y recupéralas desde sesiones y editores con acceso al mismo espacio de nombres.",
    },
    ja: {
      title: "VS CodeのGitHub Copilotで共有メモリを使う",
      description:
        "VS CodeのGitHub CopilotをMCPでLoomに接続します。同じ名前空間へのアクセス権を持つセッションやエディター間でプロジェクトの判断を共有できます。",
    },
    ko: {
      title: "VS Code의 GitHub Copilot에 공유 메모리 연결",
      description:
        "VS Code의 GitHub Copilot을 MCP로 Loom에 연결합니다. 같은 네임스페이스에 접근하는 세션과 편집기에서 프로젝트 결정을 공유할 수 있습니다.",
    },
    "pt-BR": {
      title: "Memória compartilhada para GitHub Copilot no VS Code",
      description:
        "Conecte o GitHub Copilot no VS Code ao Loom via MCP. Guarde decisões do projeto e recupere-as em sessões e editores com acesso ao mesmo namespace.",
    },
    "zh-CN": {
      title: "为 VS Code 中的 GitHub Copilot 添加共享记忆",
      description:
        "通过 MCP 将 VS Code 中的 GitHub Copilot 连接到 Loom，在有权访问同一命名空间的会话和编辑器间共享项目决策。",
    },
  },
  "concepts/memory.md": {
    "pt-BR": {
      title: "Como funciona a memória agêntica",
      description:
        "Veja como o Loom guarda fatos, eventos e procedimentos para agentes de IA. Use a memória de trabalho para retomar tarefas e consulte o histórico das revisões.",
    },
    "zh-CN": {
      title: "智能体记忆：工作记忆与长期记忆",
      description:
        "了解 Loom 如何为 AI 智能体保存事实、事件和流程，使用工作记忆恢复任务，并通过来源和版本记录追踪长期记忆的变化。",
    },
  },
  "index.md": {
    ja: {
      title: "エージェントメモリとコード検索のドキュメント",
      description: "AIエージェント向けの共有メモリ、文書検索、コードインデックスを備えたセルフホスト型MCPサーバー。",
    },
    ko: {
      title: "에이전트 메모리 및 코드 검색 문서",
      description: "AI 에이전트를 위한 공유 메모리, 문서 검색, 코드 인덱스를 제공하는 자체 호스팅 MCP 서버.",
    },
    "pt-BR": {
      title: "Documentação de memória e busca de código",
      description:
        "Memória compartilhada, busca em documentos e índice de código para agentes de IA em um servidor MCP auto-hospedado.",
    },
    "zh-CN": {
      title: "智能体记忆与代码搜索文档",
      description: "面向 AI 智能体的自托管 MCP 服务器，提供共享记忆、文档检索和代码索引。",
    },
  },
  "use-cases/index.md": {
    es: {
      title: "Casos de uso de memoria y búsqueda",
      description:
        "Casos de uso de Loom para agentes de IA: memoria compartida, búsqueda de documentos e índice de código.",
    },
  },
  "use-cases/agent-memory.md": {
    es: {
      title: "Memoria compartida para agentes de programación",
      description:
        "Comparte decisiones y lecciones entre agentes de programación y sesiones con un servidor MCP autoalojado y permisos por espacio de nombres.",
    },
    ja: {
      title: "コーディングエージェントの共有メモリ",
      description:
        "セルフホスト型MCPサーバーで、開発チームの判断や教訓をエージェントとセッションの間で共有し、名前空間ごとにアクセスを管理します。",
    },
    ko: {
      title: "코딩 에이전트를 위한 공유 메모리",
      description:
        "자체 호스팅 MCP 서버에서 코딩 에이전트와 세션 간에 결정과 교훈을 공유하고 네임스페이스별로 접근을 관리합니다.",
    },
    "pt-BR": {
      title: "Memória compartilhada para agentes de código",
      description:
        "Compartilhe decisões e aprendizados entre agentes de programação e sessões com um servidor MCP auto-hospedado e permissões por namespace.",
    },
    "zh-CN": {
      title: "AI 编程智能体的共享记忆",
      description: "通过自托管 MCP 服务器，在编程智能体和会话之间共享决策与经验，并按命名空间管理访问权限。",
    },
  },
  "use-cases/multi-agent.md": {
    es: {
      title: "Memoria y traspaso de tareas entre agentes",
      description:
        "Coordina el trabajo entre agentes con memoria compartida, colas de tareas persistentes y registros de decisiones que indican quién escribió cada dato.",
    },
    ja: {
      title: "マルチエージェントの共有メモリとタスク引き継ぎ",
      description:
        "共有メモリ、永続的なタスクキュー、決定の記録を使い、複数のエージェント間で作業とその背景を引き継ぎます。",
    },
    ko: {
      title: "멀티 에이전트 공유 메모리와 작업 인계",
      description: "공유 메모리, 영속 작업 큐, 결정 기록으로 여러 에이전트가 맥락을 보존하면서 작업을 인계합니다.",
    },
    "pt-BR": {
      title: "Memória e passagem de tarefas entre agentes",
      description:
        "Passe tarefas de um agente para outro com memória compartilhada, filas persistentes e registros que identificam quem tomou cada decisão.",
    },
    "zh-CN": {
      title: "多智能体的共享记忆与任务交接",
      description: "借助共享记忆、持久任务队列和决策记录，在多个智能体之间交接任务并保留工作背景。",
    },
  },
  "use-cases/autonomous-agents.md": {
    es: {
      title: "Memoria auditable para agentes autónomos",
      description:
        "Los agentes autónomos pueden recuperar hechos versionados, reutilizar procedimientos, auditar su origen y borrar datos dentro de un ámbito definido.",
    },
    ja: {
      title: "自律型エージェントの長期メモリと監査",
      description:
        "自律型エージェントが履歴付きの事実を検索し、手順を再利用できるメモリです。書き込みの監査と範囲を指定したデータ消去にも対応します。",
    },
    ko: {
      title: "자율 에이전트를 위한 장기 메모리와 감사",
      description:
        "자율 에이전트가 변경 이력이 있는 사실을 검색하고 절차를 재사용합니다. 기록의 출처를 감사하고 지정된 범위의 데이터를 삭제할 수 있습니다.",
    },
    "pt-BR": {
      title: "Memória auditável para agentes autônomos",
      description:
        "Agentes autônomos podem recuperar fatos versionados, reutilizar procedimentos, auditar quem registrou cada fato e excluir dados por escopo.",
    },
    "zh-CN": {
      title: "自主智能体的长期记忆与审计",
      description: "自主智能体可以检索带版本的事实、复用流程、审计写入来源，并删除指定范围内的数据。",
    },
  },
  "use-cases/memory-linked-to-code.md": {
    es: {
      title: "Memoria vinculada al código para agentes de IA",
      description:
        "Vincula las lecciones de los agentes a funciones, clases y archivos. Consulta su estado cuando el código cambia, se mueve o se elimina.",
    },
    ja: {
      title: "コードの変更を追跡するエージェントメモリ",
      description:
        "エージェントの教訓を関数、クラス、ファイルに紐づけ、リファクタリングやコードの変更・削除に応じて状態を確認できます。",
    },
    ko: {
      title: "코드 변경을 추적하는 에이전트 메모리",
      description:
        "에이전트의 교훈을 함수, 클래스, 파일에 연결하고 리팩터링이나 코드 변경·삭제 이후 연결 상태를 확인합니다.",
    },
    "pt-BR": {
      title: "Memória de agentes vinculada ao código",
      description:
        "Vincule decisões e aprendizados a funções, classes e arquivos. Verifique as referências quando o código mudar, for movido ou for removido.",
    },
    "zh-CN": {
      title: "跟踪代码变化的智能体记忆",
      description: "将智能体积累的经验关联到函数、类和文件，在重构、代码变更或删除后检查关联状态。",
    },
  },
  "use-cases/self-hosted-clickhouse.md": {
    es: {
      title: "Memoria de agentes con SQL en ClickHouse",
      description:
        "Despliega Loom en tu infraestructura con Docker o Kubernetes y consulta con SQL la memoria de los agentes almacenada en ClickHouse.",
    },
    ja: {
      title: "ClickHouseでSQL検索できるエージェントメモリ",
      description:
        "DockerまたはKubernetesでLoomをセルフホストし、ClickHouseに保存したエージェントメモリをSQLで検索できます。",
    },
    ko: {
      title: "ClickHouse에서 SQL로 조회하는 에이전트 메모리",
      description:
        "Docker 또는 Kubernetes로 Loom을 자체 호스팅하고 ClickHouse에 저장된 에이전트 메모리를 SQL로 조회합니다.",
    },
    "pt-BR": {
      title: "Memória de agentes com SQL no ClickHouse",
      description:
        "Hospede o Loom na sua infraestrutura com Docker ou Kubernetes e consulte em SQL a memória dos agentes armazenada no ClickHouse.",
    },
    "zh-CN": {
      title: "用 SQL 查询 ClickHouse 中的智能体记忆",
      description: "通过 Docker 或 Kubernetes 自托管 Loom，并使用 SQL 查询 ClickHouse 中存储的智能体记忆。",
    },
  },
  "use-cases/document-search.md": {
    es: {
      title: "RAG con citas para documentos internos",
      description:
        "Permite que los agentes de IA busquen en documentos internos mediante MCP. Recupera pasajes con su fuente y revisión y limita el acceso por espacio de nombres.",
    },
    ja: {
      title: "社内文書を引用付きで検索するRAG",
      description:
        "AIエージェントがMCPで社内文書を検索し、根拠となる箇所と文書の版を確認できます。名前空間ごとにアクセスを制御します。",
    },
    ko: {
      title: "출처를 인용하는 사내 문서 RAG 검색",
      description:
        "AI 에이전트가 MCP로 사내 문서를 검색하고 근거가 되는 구절과 문서 버전을 확인합니다. 네임스페이스별로 접근을 제한합니다.",
    },
    "pt-BR": {
      title: "RAG com citações para documentos internos",
      description:
        "Pesquise documentos internos com agentes de IA via MCP. Recupere trechos com fonte e revisão e controle o acesso por namespace.",
    },
    "zh-CN": {
      title: "带引用的内部文档 RAG 检索",
      description: "让 AI 智能体通过 MCP 搜索内部文档，获取带来源和版本的文本片段，并按命名空间控制访问权限。",
    },
  },
  "use-cases/code-intelligence.md": {
    es: {
      title: "Búsqueda de código con MCP para agentes",
      description:
        "Busca en repositorios Git indexados por MCP, identifica definiciones y llamadas y comprueba la revisión del código que respalda la respuesta.",
    },
    ja: {
      title: "MCPによるコーディングエージェントのコード検索",
      description:
        "MCPでインデックス済みのGitリポジトリを検索し、定義や呼び出し元、回答の根拠となるコードのリビジョンを確認できます。",
    },
    ko: {
      title: "코딩 에이전트를 위한 MCP 코드 검색",
      description: "MCP로 인덱싱된 Git 저장소를 검색하고 정의, 호출 위치, 답변의 근거가 된 코드 리비전을 확인합니다.",
    },
    "pt-BR": {
      title: "Busca de código via MCP para agentes",
      description:
        "Pesquise repositórios Git indexados por MCP e confira definições, chamadas e a revisão do código que fundamenta a resposta.",
    },
    "zh-CN": {
      title: "面向编程智能体的 MCP 代码搜索",
      description: "通过 MCP 搜索已建立索引的 Git 仓库，查看定义、调用方，以及为回答提供依据的代码版本。",
    },
  },
};
const excluded = new Set([
  ".blume",
  ".blume-content",
  ".blume-verify",
  ".vercel",
  "dist",
  "node_modules",
  "components",
  "pages",
  "blume.config.ts",
  "package-lock.json",
  "package.json",
  "vercel.json",
]);

async function prepareContent() {
  rmSync(content, { recursive: true, force: true });
  rmSync(docsMedia, { recursive: true, force: true });
  for (const entry of readdirSync(root)) {
    if (!excluded.has(entry)) cpSync(join(root, entry), join(content, entry), { recursive: true });
  }
  for (const locale of locales) {
    const translatedDocs = join(repository, "i18n", locale, "docs");
    if (existsSync(translatedDocs)) cpSync(translatedDocs, join(content, locale), { recursive: true });
  }

  const files = contentFiles(content);
  const englishSources = new Set(
    files
      .map((file) => relative(content, file).split(sep).join("/"))
      .filter((file) => !locales.includes(file.split("/")[0])),
  );
  const publicPages = JSON.parse(readFileSync(join(docs, "public-pages.json"), "utf8"));
  const { contentDates, productDates } = readSiteDates(repository, publicPages);
  writeFileSync(join(content, ".product-dates.json"), JSON.stringify(productDates));
  for (const file of files) {
    const text = readFileSync(file, "utf8");
    const preparedPath = relative(content, file).split(sep).join("/");
    const [locale, ...source] = preparedPath.split("/");
    const sourcePath = source.join("/");
    const localized = locales.includes(locale);
    const linked = localized ? rewritePublishedDocLinks(text, englishSources) : text;
    const rendered = localized && sourcePath === "index.md" ? prepareLocalizedLandingDemo(linked) : linked;
    const original = localized ? `i18n/${locale}/docs/${sourcePath}` : `docs/${preparedPath}`;
    const prepared = withContentMetadata(normalizeTranslatedTitle(rendered, localizedSeo[sourcePath]?.[locale]), {
      date: contentDates.get(original),
      locale: localized ? locale : "en",
    });
    if (prepared !== text) writeFileSync(file, prepared);
  }

  const packagePath = fileURLToPath(import.meta.resolve("blume/package.json"));
  const codemod = join(dirname(packagePath), "skills", "blume-migrate", "scripts", "mkdocs-codemod.mjs");
  const result = spawnSync(process.execPath, [codemod, "--write", content], {
    cwd: dirname(root),
    stdio: "inherit",
  });

  if (result.status !== 0) process.exit(result.status ?? 1);
  if (!existsSync(join(content, "index.md"))) process.exit("Blume content preparation produced no index page.");

  for (const file of contentFiles(content)) {
    let text = readFileSync(file, "utf8")
      .replaceAll("({name, value})", "(`{name, value}`)")
      .replaceAll("({name, clear: true})", "(`{name, clear: true}`)")
      .replaceAll("SQL <-> C++", "SQL ↔ C++");
    text = rewriteSourceLinks(text, relative(content, file));
    writeFileSync(file, text);
  }
  await prepareDocsScreenshot();
}

async function prepareDocsScreenshot() {
  const landing = join(content, "index.md");
  const text = readFileSync(landing, "utf8");
  const screenshot = /^!\[([^\n]*)\]\(assets\/screenshots\/document-search\.webp\)$/m;
  const match = screenshot.exec(text);
  if (!match) return;

  const image = sharp(join(root, "assets", "screenshots", "document-search.webp"));
  const { width, height } = await image.metadata();
  const widths = [360, 480, 720, 960, 1080].filter((candidate) => candidate < width).concat(width);
  mkdirSync(docsMedia, { recursive: true });
  const candidates = await Promise.all(
    widths.map(async (candidate) => {
      const bytes = await image.clone().resize(candidate).webp().toBuffer();
      const hash = createHash("sha256").update(bytes).digest("hex").slice(0, 12);
      const name = `document-search-${candidate}.${hash}.webp`;
      writeFileSync(join(docsMedia, name), bytes);
      return { url: `/docs/docs-media/${name}`, width: candidate };
    }),
  );
  const fullSize = candidates.at(-1).url;
  const srcset = candidates.map(({ url, width }) => `${url} ${width}w`).join(", ");
  // Match the documentation column's maximum width and mobile gutters.
  const sizes = "(max-width: 45rem) calc(100vw - 3rem), 42rem";
  const markup = `<img src="${fullSize}" srcset="${srcset}" sizes="${sizes}" alt="${escape(match[1])}" width="${width}" height="${height}" loading="lazy" decoding="async" data-zoom-src="${fullSize}" />`;
  writeFileSync(
    landing,
    text.replace(screenshot, () => markup),
  );
}

export function prepareLocalizedLandingDemo(text) {
  const media = "../assets/screenshots/clip-assistant-answer.webm";
  const video =
    /<video src="https:\/\/github\.com\/ClickHouse\/loom\/raw\/main\/docs\/assets\/screenshots\/clip-assistant-answer\.webm" ([^>]+)><a href="[^"]+">([^<]+)<\/a><\/video>/;
  return text.replace(video, (_, attributes, label) => {
    const onDemand = attributes.replace(" autoplay loop ", ' preload="none" ');
    const player = `<video src="${media}" ${onDemand}><a href="${media}">${label}</a></video>`;
    return onDemand === attributes ? player : `<details>\n<summary>${label}</summary>\n${player}\n</details>`;
  });
}

function contentFiles(directory) {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name);
    if (statSync(path).isDirectory()) return contentFiles(path);
    return path.endsWith(".md") || path.endsWith(".mdx") ? [path] : [];
  });
}

function publishedDocUrl(url, englishSources) {
  if (!url.startsWith(docsBlob)) return;
  const match = /^([^?#]+\.mdx?)(#.*)?$/.exec(url.slice(docsBlob.length));
  if (!match) return;
  const [, encodedPath, fragment = ""] = match;
  if (/^#L\d+(?:-L\d+)?$/.test(fragment)) return;
  let source;
  try {
    source = decodeURIComponent(encodedPath);
  } catch {
    return;
  }
  if (!englishSources.has(source)) return;
  const slug = encodedPath.replace(/\.mdx?$/, "").replace(/(^|\/)index$/, "");
  return `${docsSite}${slug ? `/${slug}` : ""}${fragment}`;
}

/** Keep the translator's choice of English fallback and send it to the published page. */
export function rewritePublishedDocLinks(text, englishSources) {
  const pending = [fromMarkdown(text)];
  const replacements = [];
  for (const node of pending) {
    if (node.children) pending.push(...node.children);
    if (node.type !== "link") continue;
    const url = publishedDocUrl(node.url, englishSources);
    if (!url) continue;
    const { start, end } = node.position;
    const labelEnd = node.children.at(-1)?.position.end.offset;
    const searchFrom = text[start.offset] === "<" ? start.offset : (labelEnd ?? start.offset);
    const offset = text.indexOf(node.url, searchFrom);
    if (offset >= 0 && offset + node.url.length <= end.offset) {
      replacements.push({ offset, end: offset + node.url.length, url });
    }
  }

  let written = "";
  let cursor = 0;
  for (const { offset, end, url } of replacements.sort((a, b) => a.offset - b.offset)) {
    written += text.slice(cursor, offset) + url;
    cursor = end;
  }
  return written + text.slice(cursor);
}

/** Move a generated translation's title into front matter for Blume to render once. */
export function normalizeTranslatedTitle(text, seo = {}) {
  if (!text.startsWith("<!-- i18n: source=")) return text;

  const lines = text.split("\n");
  const heading = lines.findIndex((line) => /^#\s+/.test(line));
  if (heading === -1) return text;

  const title = lines[heading].replace(/^#\s+/, "").replace(/\s+#+\s*$/, "");
  lines.splice(heading, 1);
  if (lines[heading - 1] === "" && lines[heading] === "") lines.splice(heading, 1);
  const searchFields = Object.entries(seo)
    .filter(([, value]) => value)
    .map(([name, value]) => `  ${name}: ${JSON.stringify(value)}`);
  const searchMetadata = searchFields.length ? `\nseo:\n${searchFields.join("\n")}` : "";
  return `---\ntitle: ${JSON.stringify(title)}${searchMetadata}\n---\n\n${lines.join("\n")}`;
}

export function rewriteSourceLinks(text, from) {
  const [locale, ...sourceParts] = from.split("/");
  const sourceFrom = locales.includes(locale) ? sourceParts.join("/") : from;
  let fenced = false;
  return text
    .split("\n")
    .map((line) => {
      if (/^\s*(```|~~~)/.test(line)) fenced = !fenced;
      if (fenced) return line;
      return line.replace(/(!?\[[^\]]*\]\()([^\s)]+)(\))/g, (whole, prefix, target, suffix) => {
        if (/^(?:[a-z]+:|\/|#)/i.test(target)) return whole;
        const [path, anchor] = target.split("#", 2);
        const resolved = normalize(join(dirname(sourceFrom), decodeURIComponent(path))).replaceAll("\\", "/");
        if (!resolved.startsWith("../")) return whole;
        const repositoryPath = resolved.replace(/^\.\.\//g, "");
        const kind = prefix.startsWith("!") ? "raw" : "blob";
        const fragment = anchor ? `#${anchor}` : "";
        return `${prefix}https://github.com/ClickHouse/loom/${kind}/main/${repositoryPath}${fragment}${suffix}`;
      });
    })
    .join("\n");
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await prepareContent();
