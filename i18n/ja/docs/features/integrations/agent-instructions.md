<!-- i18n: source=docs/features/integrations/agent-instructions.md blob=66910fd5b55982ca9d0f8f554d606e94b2c0f60c model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-instructions.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-instructions.md), which is authoritative.

<a id="agent-instructions"></a>

# エージェント向け指示

エージェントの指示ファイルに短いブロックを置くと、Loom のコンテキストを読み込むタイミング、事実を保存するタイミング、grep やファイル読み取りの代わりにコードインデックスを使うタイミングをエージェントに伝えられます。このページでは、そのブロックと、各クライアントがそれを読み込むファイルを一度だけ示します。ブロックを貼り付け、2 つの名前空間を `code.repos`（下記）から取得して埋め、リポジトリと一緒にコミットしてください。

Claude Code と Codex のプラグインは、フックとスキルを通じてすでにこれを行っています（[Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#skills)、[Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md#skills)）。このブロックは、それ以外のすべてのクライアント、およびプラグインなしで接続した Claude Code や Codex 向けです。ブロックには `coding` ツールセットに含まれるツールの名前が使われているため、クライアントは `?toolset=coding` で接続してください。対応しているクライアントでは `loom install --client <name> --toolset coding` を使い、それ以外の場合は URL に `?toolset=coding` を付けます（[MCP クライアント](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)）。

<a id="the-block"></a>

## ブロック

[Cline フック](https://github.com/ClickHouse/loom/blob/main/integrations/cline/README.md)を使う場合は、コピーしたブロックのうち「Every task:」で始まる段落を「Every task: make these two calls.」に置き換えて、2 つの呼び出しが両方とも必須のままになるようにします。Cline はフックのテキストを、そのフックが実行されたツールの後でしかモデルに表示しません。そのため、プライマーはタスクの最初のツール呼び出しの後に届き、編集に対するファイルブロックは編集の後に届きます。

```markdown
## Loom: memory and code index

Loom is connected as an MCP server; its tools are named `loom_*`. A tool you do not see
listed runs through `loom_op` with the op's dotted name: on a connection without the
coding toolset, `loom_code_symbol` is `code.symbol` and `loom_working_memory_set` is
`working_memory.set`. `loom_op` with `{"op": "help", "query": "<words>"}` finds them.

Every task: make these two calls unless a Loom hook already put Loom's context in this
session (the Claude Code and Codex plugins do). Without one they are not optional.

- At the start of each task, before planning: `loom_context_pack` with the task as
  `query`. It returns the decisions, constraints and memories that bear on it.
- Before editing a file: `loom_op` with `op` set to `memory.for_file` and `arguments`
  holding `path` (the file's repo-relative path) and `extra_namespaces`
  (`["<team-namespace>"]`). Follow every constraint and decision it returns.

Memory

- To recall something mid-task, call `loom_search` with the topic, file or error. Treat
  `abstain` as "nothing relevant". A hit whose `code_state` is `changed`, `unresolved`
  or `missing` is about code that changed or went away since it was written: check the
  code before relying on it.
- When the user states a decision, preference, constraint or correction, save it with
  `loom_memory_set` right away: one plain-text fact per call under a readable key
  (`<scope>/<kind>/<id>`), dates as calendar dates. Never save a guess, your reasoning or
  the conversation.
- A rule the user states for the whole team about a file: `loom_memory_set` with
  `namespace` `<team-namespace>`, the file in `files` and the tag `constraint`.
- A fact that changed: `loom_memory_update` on the same key, in the namespace the row
  lives in, never a second copy. A team rule or decision someone else wrote is not yours
  to update: follow the refusal's `hint`.
- Your plan and checklist for this task: `loom_working_memory_set`, not `loom_memory_set`.
- Before reporting done, one pass over what should outlive the session.

Code

- This repository's code namespace is `<code-namespace>`; pass it as `namespace` on
  every `loom_code_*` call.
- Where X is defined: `loom_code_symbol`. Who calls X: `loom_code_callers`. A concept
  with no exact keyword: `loom_code_search`. An exact string: `loom_code_grep`. What is
  in a file: `loom_code_outline`, then read one span. What a change breaks:
  `loom_code_impact`.
- Grep your own uncommitted files and repositories the index does not cover. When a code
  tool answers `abstain: true`, grep instead of retrying with synonyms.
- A code answer's `lessons` are memories about its top files: follow them, and check the
  code of one whose `code_state` is `changed`, `unresolved` or `missing` before relying on
  it.
```

`code.repos` は、トークンが読み取れる、または参加を申請できるリポジトリを、インデックス済みのブランチごとに 1 行で返します。

```bash
curl -s -X POST https://loom.example.com/v1/code.repos \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"repo": "<owner>/<name>", "fields": ["branch", "primary_namespace", "readable"]}'
```

`<code-namespace>` は、このファイルのコピーがコミットされているブランチの行の `namespace` に、`<team-namespace>` はその行の `primary_namespace`（チームのファイルルールと決定が置かれるトランクブランチのもの）に置き換えます。トランクブランチでは、この 2 つは同じです。行がないリポジトリ（インデックスされていない、または `code.repos` がコードインテリジェンスは無効だと答える場合）や、行が `readable: false` となっているリポジトリ（このトークンでは読み取れないが、アクセスの申請や取り込みができるよう一覧に出ているもの）では、`extra_namespaces`、チームルールの行、Code セクションを除いたブロックを使います。

<a id="where-each-client-reads-it"></a>

## 各クライアントが読み込む場所

ブロックはどのクライアントでも同じです（Cline フックの上記の 1 段落分の変更を除く）。異なるのはファイルとそのヘッダーだけです。これらのクライアントの多くは `AGENTS.md` も読み込むため、1 つのファイルで複数のクライアントをまかなえる場合は、コピーを 1 つだけ置いてください。ブロックを含む 2 つのファイルを読み込むクライアントでは、ブロックが 2 回表示されます。コンテキストを余分に消費しますが、動作は変わりません。

| クライアント                                                   | ファイル                                                               | ブロックの前のヘッダー                                                       |
| -------------------------------------------------------------- | ---------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| プラグインなしの Codex、および `AGENTS.md` を読み込む任意のエージェント | リポジトリのルートにある `AGENTS.md`                                   | なし                                                                         |
| Zed                                                            | 最初に見つかった指示ファイル（下記参照）                               | なし                                                                         |
| プラグインなしの Claude Code                                   | リポジトリのルートにある `CLAUDE.md`（下記参照）                       | なし                                                                         |
| Cursor                                                         | `.cursor/rules/loom.mdc`                                               | `---`、`description: Loom memory and code index`、`alwaysApply: true`、`---` |
| Devin Desktop（Windsurf）                                      | `.devin/rules/loom.md`（Windsurf ビルドでは `.windsurf/rules/loom.md`） | `---`、`trigger: always_on`、`---`                                           |
| Cline                                                          | `.clinerules/loom.md`                                                  | なし                                                                         |
| VS Code（GitHub Copilot）                                      | `.github/copilot-instructions.md`                                      | なし                                                                         |

Zed は最初に見つかった指示ファイルだけを読み込みます（`AGENTS.md` より前に `.rules`、`.cursorrules` などを確認します）。そのため、ブロックはそのファイルに置きます。リポジトリに指示ファイルがない場合は `AGENTS.md` に置きます。

Claude Code は `AGENTS.md` を読み込みません。リポジトリにそのファイルがある場合は、ブロックを `AGENTS.md` に置き、`CLAUDE.md` から `@AGENTS.md` の行でインポートしてください。

ヘッダーの各行は、それぞれ独立した行に記述します。たとえば Cursor のファイルは次のように始まります。

```markdown
---
description: Loom memory and code index
alwaysApply: true
---

## Loom: memory and code index
```

既存の `AGENTS.md`、`CLAUDE.md`、または Copilot のファイルの内容はそのまま残し、ブロックをその中の 1 つのセクションとして追加してください。

<a id="why-these-rules"></a>

## これらのルールの理由

フックのないセッションでは、何も想起を促さず、一覧に載っているメモリツールは数ある選択肢の一つにすぎません。そのためブロックは、プラグインのフックと同じように、コンテキストの読み込みをすべてのタスクの一部にします。

各行は、プラグインが教える内容、または Loom の応答が運ぶ内容を短くまとめたものです。

- タスクの開始時と編集の前にコンテキストを読み込む：プラグインのセッション開始フックとファイルコンテキストフックが、`context.pack` と `memory.for_file` を呼び出します。
- 述べられた事実を、そのつど想起して保存する：
  [`loom-memory`](https://github.com/ClickHouse/loom/blob/main/integrations/claude-code-plugin/skills/loom-memory/SKILL.md)
  スキル。
- grep の前にコードインデックスを使い、インデックスで答えられない場合に grep を使う：
  [`loom-code`](https://github.com/ClickHouse/loom/blob/main/integrations/claude-code-plugin/skills/loom-code/SKILL.md)
  スキル。プラグインのコードシャントがこれを強制します。
- コードの応答が運ぶメモリを読む：その `lessons`
  （[コードにリンクされたメモリ](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#memories-linked-to-code)）。プラグインのセッションでは、同じ応答でこれを受け取れます。

ブロックの最後のメモリ行は、タスク終了時のレビューを表します。これを明示するには、[エージェント統合ガイド](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md#7-follow-the-memory-skills-review)の質問をコピーして、ブロックの下に置きます。スキルには、ブロックが省いた詳細（レシートのフィールド、ドキュメントとメモリの違い、停止ルール）が含まれています。この詳細が必要な場合は、`loom` スキルと `loom-code` スキルをブロックと並べてインストールできます（[コーディングエージェント](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md#coding-agents)）。`loom` は、メモリのループ、書き込みの形式、レシート、タスク終了時のレビューを扱います。これらにはフックがないため、ブロックの二つの呼び出しの代わりにはなりません。

<a id="related"></a>

## 関連ドキュメント

- [MCP クライアント](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)：各クライアントの接続方法。
- [ツールセットの選択](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md#choosing-a-toolset)。
- [インテグレーション](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md)。
