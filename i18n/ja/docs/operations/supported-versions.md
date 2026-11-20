<!-- i18n: source=docs/operations/supported-versions.md blob=32631f7ef1ce0afc4871e89ec501bcad37732e46 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/operations/supported-versions.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/supported-versions.md), which is authoritative.

<a id="supported-versions"></a>

# サポート対象バージョン

リリース N と N−1 には修正が提供されます。`X.Y` の SDK は、`X.Y` または `X.(Y−1)` のサーバーと組み合わせて動作します。Loom は、サーバー、2 つのイメージ、Web アプリ、2 つの SDK、ルートの skills、Helm チャートの `appVersion` に対して、単一のバージョンを配布します（[リリース手順](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md)）。このページでは、コーディングエージェント用プラグインについてと、何かを削除する変更をどのように告知するかについても説明します。

<a id="release-lines"></a>

## リリースライン

| ライン | 提供される内容                                                                                   |
| ---- | ------------------------------------------------------------------------------------------------- |
| N    | 最新のマイナーリリース（`X.Y`）：すべての修正。待てない場合はパッチリリース（`X.Y.(Z+1)`）として提供されます |
| N−1  | その1つ前のマイナーリリース：セキュリティ修正、およびデータ損失や誤った回答に対する修正がパッチとして提供されます |
| N−2  | なし：N または N−1 にアップグレードしてください                                                  |

マイナーリリースは約 2 週間ごとに `main` から作られます。パッチは、そのラインの `release/X.Y` ブランチから作られ、修正は先に `main` にマージされます。アップグレードは[アップグレード](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md)に従います。隣り合う 2 つのバージョンは、一方が他方に置き換わる間、並行して稼働できます（[ローリングアップグレード](https://github.com/ClickHouse/loom/blob/main/docs/architecture/rolling-upgrades.md)）。

<a id="sdks-and-servers"></a>

## SDK とサーバー

SDK（PyPI の `clickhouse-loom` と npm の `@clickhouse/loom`）は、リリースごとにサーバーと同じバージョンで公開されます。npm の `latest` は最も高い正式版を指し、古いラインへのパッチには `release-X.Y` の dist-tag が付きます。古いサーバーに対しては、次のようになります。

- SDK が送信するフィールドを古いサーバーが認識しない場合、リクエストは処理され、そのフィールドは応答の `ignored_args` に記載されます。ただし、状態を変更する op では、サーバーがそのフィールドのいずれかのつづり間違いと判断するキー（近い名前、同義語、フィールド名の一部の単語）は `422 UNKNOWN_ARGUMENT` で拒否され、何も書き込まれません（[不明なフィールドと削除されたフィールド](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#unknown-and-removed-fields)）。
- 古いサーバーが提供しない操作は `404` で応答し、SDK はこれを `NotFound` として送出します（Python では `loom.errors.NotFound`、TypeScript では `NotFound`）。そのため、実行できないサーバーに対する呼び出しが黙って成功することはありません。同じエラーは行が存在しない場合も意味するため、両者を区別する必要があるクライアントは、起動時に `ops.capabilities` を読み取ります（[デプロイメントが提供する機能の確認](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#discovering-what-a-deployment-serves)）。

メジャーリリース（`X.0`）では、直前の `(X−1).Y` サーバーに対してこのルールが破られる場合があります。`X.0` の SDK に必要な条件は、そのリリースノートに記載されます。

古い SDK は、呼び出す機能をサーバーが提供し続ける限り、新しいサーバーに対して動作し続けます。削除については、以下のルールに従います。

<a id="coding-agent-plugins"></a>

## コーディングエージェント用プラグイン

Claude Code と Codex のプラグインは独自のバージョンを持ち、フックがサポートする最も古いサーバーを示します（`lib/server-version.mjs` の `MIN_SERVER_VERSION`）。古いサーバーに対するセッションは、その旨を伝える `loom:` の 1 行で始まり、プラグインは可能な範囲で動作し続けます（[Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md)）。Claude Code プラグインには Claude Code 2.1.139 以降が必要です。これは、フックの `args` をシェルなしで実行する最初のバージョンです。

<a id="removing-something"></a>

## 削除について

削除は、削除を行うリリースの 1 つ前のマイナーリリースのリリースノートにある「What changed for clients」の項に、リリースのプルリクエストで手作業により記載して告知されます。削除までの間、サーバーは古いフィールドや op を提供し続けます。削除後は、削除されたリクエストフィールドも引き続き受け付けられて `ignored_args` に記載されるため、それを送信するクライアントは失敗せずに変更を知ることができます。削除された op は `404`（`NotFound`）で応答し、これは `ops.capabilities` によって行の欠落と区別できます。

<a id="release-artifacts"></a>

## リリース成果物

すべてのリリースイメージは、実行されるアーキテクチャ上でビルドされ、修正が存在する重大な脆弱性についてスキャンされ、プロベナンスと SBOM とともに公開されます。各イメージのマルチアーキテクチャインデックスには、リリースワークフローによるキーレス署名が付きます。`cosign verify` コマンドについては[コンテナイメージ](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#container-images)を参照してください。GitHub Release 上の各ファイルも同じ方法で署名され、隣に `<file>.sigstore.json` として置かれます。`cosign verify-blob` コマンドについては[リリースファイル](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#release-files)を参照してください。
