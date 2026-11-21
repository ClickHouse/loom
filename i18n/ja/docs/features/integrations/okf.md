<!-- i18n: source=docs/features/integrations/okf.md blob=7d8ba9d5d921d351c46c4592c82a48277bb4f2cb model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/okf.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/okf.md), which is authoritative.

<a id="okf-bundles"></a>

# OKF バンドル

名前空間は、OKF (Open Knowledge Format) v0.2 のバンドルとしてレンダリングできます。バンドルは、メモリごとに 1 つの Markdown ファイル（YAML frontmatter 付き）、各ディレクトリの概念を 1 行の説明つきで列挙する `index.md`、変更履歴の `log.md` で構成されます。CLI はクライアント側で動作し、HTTP API を介して処理します。バンドルは diff を取ったり、プルリクエストでレビューしたり、オフラインで読んだりできます。保存先は引き続き Loom です。

バンドルは射影であり、2 つ目のストアではありません。バンドルは名前空間から再生成され、編集は通常の書き込みパス（競合検出、ブラックリスト、監査）を通って戻ります。

```bash
loom export-okf --namespace <ns> --out ./knowledge        # render
loom okf-lint ./knowledge                                 # offline check: exit 2 critical, 1 warnings, 0 clean
loom import-okf ./knowledge --namespace <ns> --dry-run    # plan the write-back
```

3 つのコマンドはいずれも `--server-url`、`--token`（デフォルトは `$LOOM_API_KEY`、次に `~/.loom/token`）、`--org`（デフォルト：トークンの `org` クレーム）を受け付けます。`okf-lint` にはこれらは不要です。すべてのフラグは [CLI リファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md)を参照してください。

<a id="export"></a>

## エクスポート

`loom export-okf` は読み取り専用です。名前空間のすべての有効な行（全エージェント分）を `memory.list` でページ送りしながら取得し、`memory.bulk_get` で完全な値を取り出して、計画したファイルを `--out` の下に書き出します。既存のコンセプトファイルは上書きされますが、ディレクトリ内のそれ以外のものには触れません。マッピングは `server/memory/domain/okf_bundle.py`（純粋関数、ユニットテスト済み）にあり、`server/cli/okf.py` がトランスポートとファイルシステムを担当します。

| メモリ                             | コンセプト                                                                                                                                                                                                                                                                                                                                                     |
| ---------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory_key`                       | ファイル名と `loom.memory_key`：キーのスラッグ（160 バイトまで）。同じ名前を別のキーが使っている場合、またはバンドルのファイル名（`index`、`log`、`README`、`AGENTS`、大文字小文字は問わない）や Windows のデバイス名（`nul`、`con`、`com1`）と衝突する場合は、キーと行 ID の `-<hash>` を付加します                                                                                       |
| `memory_type`                      | ディレクトリ（タイプを小文字にしたスラッグ。ファイル名と同様に長さを制限します。`evidence`、`digests`、`staging`、`node_modules`、バンドルファイルのフルネーム（`index.md`、`log.md`）、または `nul` などの Windows デバイス名の場合は `type-<slug>`）と `type`：`semantic` → Fact、`procedural` → Procedure、`preference` → Preference、`episodic` → Event、それ以外は先頭を大文字にした形 |
| 値                                 | 本文：テキストはそのまま、JSON はフェンス付きブロック                                                                                                                                                                                                                                                                                                          |
| `tags`                             | `tags`                                                                                                                                                                                                                                                                                                                                                         |
| エージェントと `created_at`        | `generated: {by: "agent:<agent>", at}`                                                                                                                                                                                                                                                                                                                         |
| 有効な `verified_by` / `verified_at` | `verified: [{by, at}]`（人間とプロセスによる証明のみ）                                                                                                                                                                                                                                                                                                         |
| `status`                           | `active` → `stable`、`conflicted` → `draft`、それ以外 → `deprecated`                                                                                                                                                                                                                                                                                           |
| `expires_at`                       | `stale_after`（レビュー期限の日付）                                                                                                                                                                                                                                                                                                                            |
| `source_resource_id`               | `sources: [{resource}]`                                                                                                                                                                                                                                                                                                                                        |
| Loom 固有のすべての情報            | `loom:` ブロック。`source_class`、`sensitivity`、`superseded_by`、`valid_to`、`content_sha256` を含みます                                                                                                                                                                                                                                                      |

**リンク。** 人が作成したエッジ（`memory.export_evidence kind=edges` で読み取る名前空間の型付き `memory.link` エッジと、置き換えられた行が暗黙に持つ `supersedes` チェーン）は、正式なフロントマター `relates_to: [{path, kind}]` として書き出され、本文の「Related Concepts」セクションにも反映されます。その後に共有エンティティのリンクが、件数を制限した対称な形で続くため、バリデータがたどれるグラフがバンドルに含まれます。どこにもリンクしないコンセプトは孤立したまま残り、CLI はそれらを隠さずに報告します。

**来歴は引き継がれます。** `loom:` ブロックには行の来歴ティアが含まれるため、ラウンドトリップによって推論が一次情報の事実に変わることはありません。

| フラグ                 | 効果                                                                                                                                                                                                                                                                                                                                                                                                  |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--include-high`       | `sensitivity: high` の行、それにリンクされた生のチャンクとダイジェスト、および high の形式（認証情報、カード番号、SSN、記載された PIN）を含む生テキストもエクスポートします。サーバーは解放した各項目を監査します。`memory.bulk_get` は保持期間を過ぎた `high` の行を返さないため、その行は名前を示したうえで除外されます。デフォルトではオフです。ミラーリポジトリは名前空間の権限付与よりも広い読者を想定するためです |
| `--no-evidence`        | コンセプトのみを出力し、バンドルがデフォルトで含める生のチャンク（`evidence/`）とセッションダイジェスト（`digests/`）を省略します                                                                                                                                                                                                                                                                     |
| `--no-links`           | 型付きエッジを省略します（共有エンティティのリンクは引き続き書き出されます）                                                                                                                                                                                                                                                                                                                          |
| `--include-tombstoned` | ソフト削除された行も `status: deprecated` としてエクスポートします。リストが全体を返さないもの（プレビューを超える値、または `--include-high` 指定時の `high` の行）は、名前を示したうえで除外されます                                                                                                                                                                                              |
| `--limit N`            | 新しい順に最大 N 行まで（スモークラン用）                                                                                                                                                                                                                                                                                                                                                             |
| `--title`, `--dry-run` | ルートインデックスのタイトル。書き込みを行わず計画のみ実行                                                                                                                                                                                                                                                                                                                                            |

<a id="lint"></a>

## リント

`loom okf-lint DIR` は、バンドルディレクトリだけを読み込みます。サーバー、ClickHouse、ネットワークは使いません。各検出結果には、id、パス、メッセージ、重大度が含まれます（`server/memory/domain/okf_lint.py`）。

| id                              | 重大度   | 意味                                                                                                 |
| ------------------------------- | -------- | ---------------------------------------------------------------------------------------------------- |
| `frontmatter`                   | critical | コンセプトにフロントマターがない、YAML を解析できない、`type` が空、または `tags` の値がリストではない |
| `required_keys`                 | critical | `title`、`description`、`loom.memory_key`、`loom.memory_type` のいずれかがない                       |
| `relates_to`                    | critical | エッジの形式が不正、またはその `.md` ターゲットがバンドル内に存在しない                              |
| `index_missing` / `okf_version` | critical | コンセプトを含むディレクトリに `index.md` がない、またはルートのインデックスに `okf_version` が宣言されていない |
| `staging`                       | critical | `staging/` ディレクトリが存在する。staging は応答に使われない                                        |
| `type_directory_mismatch`       | warning  | `loom.memory_type` が、ファイルのあるフォルダと一致しない                                            |
| `pointer_unmounted`             | warning  | `relates_to` のパスが、バンドル外のコンセプトを指す `loom://org/namespace/key` ポインタである        |
| `unknown_relation`              | warning  | Loom の標準の動詞に含まれない `relates_to` の種別                                                    |
| `edited_since_export`           | warning  | 本文のハッシュが `loom.content_sha256` と一致しない（コンセプトまたは raw チャンクが手作業で編集された） |
| `derived_needs_confidence`      | warning  | `source_class: derived` で confidence が 1.0 である。推論が事実として書かれている                    |
| `high_sensitivity_present`      | warning  | 機微度が high の行（`loom.sensitivity: high` または `sensitivity:high` タグ）が持ち出された           |
| `index_listing`                 | warning  | ディレクトリのインデックスまたはルートのインデックスに一覧されていないコンセプトがある               |
| `drift`                         | warning  | ルートのインデックスの `loom.corpus_digest` が、コンセプトファイルと一致しない                      |
| `orphans`                       | info     | どこにもリンクせず、どこからもリンクされていないコンセプト（データの性質であり、欠陥ではない）       |

終了コードは、コンパイルステップが必要とする内容に沿っています。critical の検出結果が 1 件でもあれば **2**、warning のみなら **1**、問題がなければ **0** です。`--strict` を指定すると、warning でも終了コードが 2 になります（orphans は情報扱いのままです）。`--json` を指定すると、機械可読なレポートを出力します。

**コーパスダイジェスト。** ルートの `index.md` に `loom.corpus_digest` がある場合、lint はこれを再計算します。コンセプトファイルだけを対象に、バンドル相対パスでソートし、各ファイルの SHA-256 の16進ダイジェストを区切りなしで連結して、その文字列の SHA-256 を求めます。一致しない場合は `drift` warning となり、ツリー内のコンセプトファイル数とインデックスの記載数が示されます。

<a id="import"></a>

## インポート

`loom import-okf DIR --namespace NS` は最初にリントを実行し、重大な指摘があるバンドルは `--force-import` を指定しない限り拒否します（`--no-lint` を指定するとこのゲートを省略します）。その後、各コンセプトを `memory.set` で書き込みます。ID はトークン、または `--agent`、`--user-id`、`--session-id` フラグから決まり、競合検出、ブラックリスト、監査ログの対象になります。書き込みのあとに `memory.link` で `relates_to` エッジを再作成します（`--no-links` を指定すると省略します）。

書き込みの前に、インポートは自身の ID で参照できる既存の行を読み取り、書き込みで送る内容と同じ内容をすでに保持している行のコンセプトをすべてスキップします。比較する項目は、値（`content_sha256`）、type、tags、confidence、`valid_at`、`valid_to`、`source_trace`、`source_class`（宣言されたクラス。なければ `imported`）で、バンドルにエンティティが含まれる場合はエンティティも比較します。すでに有効なエッジもスキップします。同じ行に対応するコンセプト（1 つのキーが複数の書き込み元向けにエクスポートされたもの）は 1 回だけ書き込まれ、最も新しく更新されたものが採用されます。同一の行を再送すると強化（reinforcement）として数えられるため、同じバンドルを 2 回インポートしても、2 回目にはコンセプトもエッジも書き込まれません。ただし例外が 2 つあります。`sources:` を持つコンセプトは、インポートが行う読み取りでソースが返されないため、実行のたびに書き込まれます。また、ターゲットが別のキーにマージするコンセプトは、実行のたびに再送され、その行を強化します（マージ済みとして数えられます）。最終行には、書き込み済み、変更なし、マージ済み、失敗のターゲット行数が表示されます。

`source_trace` は `okf:<path inside the bundle>` なので、バンドルがディスク上のどこにあるかは影響しません。実行のたびに新しいディレクトリへエクスポートしても構いません。

バンドルの `evidence/` と `digests/` は `memory.import_evidence` で書き戻され（`--no-evidence` を指定すると省略します）、変更の有無にかかわらず、インポートのたびにインポート先サーバーが再埋め込みを行います。そのため、定期実行のインポートでは `--no-evidence` を指定してください。テキストの機微度が高いと判定されるチャンクやダイジェスト（認証情報、カード番号、SSN、明示された PIN）、およびそれらの一部がチャンク境界で分断されて含まれるチャンクは、ベクトルなしで書き込まれ、検索できません（[機微度](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#sensitivity)）。

| フラグ            | 効果                                                                                                                                                                                  |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--verify`        | フロントマターに `human:` 検証を持つコンセプトを、`memory.verify` により**インポートを実行した呼び出し元として**再証明します                                                              |
| `--keep-sessions` | 各コンセプトを `loom.session_id` に記録されたセッションで書き込みます。複数のセッションにあったキーは複数の行のままになります。`--session-id` はフォールバックです                            |
| `--dry-run`       | 書き込みを一切送信せずに、ターゲットの既存の行（読み取ります）に対する書き込み計画を作成します                                                                                                |
| `--prune`         | この `--agent` とトークンによる過去のインポートが書き込んだ行（`okf:` の `source_trace` を持つもの）のうち、キーがバンドルに存在しなくなったものを `memory.delete_version` で削除します。`--agent` が必要です |

`--prune` を使って名前空間にインポートする各バンドルには、それぞれ専用の `--agent` が必要です。prune は、そのエージェントのトークンで書き込まれた `okf:` トレースを持つ行のうち、バンドルに存在しないものを削除します。`--prune` は、コンセプトを持たないバンドルと、コンセプトファイルのハッシュがルートインデックスの `loom.corpus_digest` と一致しないバンドルを拒否するため、手で編集したバンドルによって削除が行われることはありません。ルートインデックスに `loom.complete: true` がないバンドルは、書き込まれますが prune はされません。エクスポートがこのマーカーを書き込むのは、すべての行を読み取れた場合に限られます。つまり、一覧取得のあとに `memory.keys` が 1 つのステートメントで読み取るすべての行と、一覧に載ったすべての行が `memory.bulk_get` から返された場合です。`--limit` で停止した場合、`--include-tombstoned`（削除済みの行をバンドルに残す）を指定した場合、または古いリリースでエクスポートした場合には、書き込まれません。各削除は、インポートの一覧取得で確認した `okf:` トレース付きのバージョンを指定して、その ID のまま `memory.delete_version` で行われます。その後に書き換えられた行や `memory.lock` でロックされた行は残り、このオペレーションを知らない古いターゲットでは何も削除されません。prune を行う場合は空のディレクトリにエクスポートしてください。エクスポートはコンセプトファイルを上書きしますが、名前空間から消えたキーのファイルは削除しないため、再利用した `--out` ディレクトリはダイジェストと一致しなくなります。[名前空間を別の Loom にコピーする](https://github.com/ClickHouse/loom/blob/main/docs/operations/okf-sync.md)手順では、エクスポートとインポートをスケジュールで実行します。

`--verify` が証明を偽造することはありません。実行者はサーバーがトークンから決定し、バンドルに記録されていた元の検証者は、来歴のためにメモリの tags に記録されます。

<a id="ci-gate"></a>

## CI ゲート

`integrations/okf-ci/okf-lint.yml` は、バンドルを保持するリポジトリ向けの GitHub Actions ワークフローです。

1. このファイルを、そのリポジトリに `.github/workflows/okf-lint.yml` としてコピーします。
2. `LOOM_REF` に、40 文字の Loom コミット SHA を設定します。これ以外の値はジョブが拒否します（ファイルは空の状態で提供されます）。ルールは Loom とともにバージョン管理されており、ブランチやタグは、チェックが成功している間にも指す先が変わる可能性があるためです。
3. `OKF_ROOT` に、バンドルのディレクトリ、つまりルートの `index.md` があるディレクトリを設定します（デフォルトは `.`）。1 つの名前空間のバンドル、またはリポジトリが保持する複数の名前空間にわたるマトリクスを指定できます。

ジョブは、バンドルの隣に `LOOM_REF` の Loom をチェックアウトし、`uv run --frozen` で lint を実行します。そのため、パッケージはそのコミットの `uv.lock` から取得されます。1 つの `LOOM_REF` は、1 組のルールと 1 組の依存関係に対応します。lint は `--json` 付きで実行され、サマリーと最初の 40 件の検出結果（重大度、id、パスの順にソート）がジョブログに出力されます。`okf-lint.json` はアーティファクトとしてアップロードされます。終了コード 2 の場合、または lint が実行されなかった場合（それ以外の終了コード）、ジョブは失敗します。警告は通過し、レポートはレビュアー向けに保持されます。

すべてのアクションは、完全なコミット SHA に固定され、バージョンがコメントとして付記されています。ワークフローが要求する権限は `contents: read` のみで、どちらのチェックアウトも、トークンを git config に保持しません（`persist-credentials: false`）。lint 自体はネットワーク接続を開かず、ClickHouse も不要です。各チェックの詳細は `integrations/okf-ci/README.md` を参照してください。

<a id="run-the-gate-locally"></a>

### ゲートをローカルで実行する

同じコマンドで、同じ結果が得られます。

```bash
git clone https://github.com/ClickHouse/loom .okf-lint-loom
git -C .okf-lint-loom checkout <LOOM_REF>
uv run --project .okf-lint-loom --frozen --python 3.12 loom okf-lint <OKF_ROOT>          # human report
uv run --project .okf-lint-loom --frozen --python 3.12 loom okf-lint <OKF_ROOT> --json   # the CI report
```

<a id="limits"></a>

## 制限事項

- **バンドルはスナップショットです。** エクスポート後に Loom で行った変更は、次回のエクスポートまでバンドルに含まれません。古いバンドルを編集する前に、再エクスポートしてください。
- **削除が反映されるのは `--prune` を指定した場合のみで、** インポートが書き込んだ行に限られます。バンドルから削除されたエッジは削除されません。
- **機密度が high の行は、`--include-high` を指定しない限り持ち出されません。** 行が high と判定されるのは、`sensitivity` フィールドが high の場合、または `sensitivity:high` タグが付いている場合です。エクスポートが機密度を読み取れなかった行や、未知のレベルとして読み取った行も持ち出されません。そのような行にリンクされた raw チャンクやセッションダイジェスト、および本文に high に該当する形式（認証情報、カード番号、SSN、明示された PIN）を含むものも同様です。raw テキストには機密度の列がないため、`LOOM_MEMORY_SENSITIVITY_AUTODETECT` の設定にかかわらず、`memory.set` が実行する検出器が判定します。high の行が持ち出された場合、lint が警告を出します。

<a id="related"></a>

## 関連情報

- [メモリの書き込み](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md)と[競合と拒否](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md)：インポートが通る処理の流れ。
- [履歴と来歴](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)。
- [エンティティグラフ](https://github.com/ClickHouse/loom/blob/main/docs/features/entity-graph.md)：`relates_to` が持つ型付きエッジ。
