<!-- i18n: source=docs/features/tracker/github-feed.md blob=81fe53135dda14b5a303c6f1f10b89c97e768376 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/github-feed.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/github-feed.md), which is authoritative.

<a id="maintain-a-github-tracker-record"></a>

# GitHub トラッカーレコードの保守

`loom tracker-feed` は、リポジトリの issue、プルリクエスト、issue コメント、インラインのレビューコメントを、チーム専用の ClickHouse データベースにコピーします。Loom は、その結果のビューを[読み取り専用のトラッカーソース](record-contract.md)として読み取ります。コピーされたコンテンツ、バックアップ、消去はチームが管理します。このジョブはモデルや埋め込みの呼び出しを行いません。

<a id="prepare-the-record"></a>

## レコードの準備

安定したデータベース UUID を持つ空のデータベースを使用します。このデータベースは Loom のデプロイメント用データベースとは別にします。専用のライターに、このデータベースでのテーブルとビューの作成、および行の読み取りと挿入の権限を付与します。ライターの認証情報はトラッカーのリーダーとは分けて管理します。

```sql
CREATE DATABASE team_feed;
CREATE USER feed_writer IDENTIFIED BY '…';
GRANT CREATE TABLE, CREATE VIEW, SELECT, INSERT ON team_feed.* TO feed_writer;
GRANT SELECT ON system.columns TO feed_writer;
GRANT SELECT ON system.databases TO feed_writer;
GRANT SELECT ON system.tables TO feed_writer;
CREATE USER feed_reader IDENTIFIED BY '…' SETTINGS readonly = 2;
GRANT SELECT ON system.columns TO feed_reader;
```

ライターのパスワードは、スケジューラのシークレット機能を通じて `LOOM_TRACKER_FEED_WRITER_PASSWORD` として渡します。CLI は、ClickHouse 接続とシークレットルートを含め、サービスを提供するプロセスと同じ Loom のデプロイメント設定で実行します。`LOOM_TRACKER_ORG` には、初期化する組織を設定します。CLI は使用前にライターに直接付与された権限を検証し、ロール、グラント オプション、このデータベース外の権限は、上記 3 つのシステムメタデータテーブルを除いて拒否します。

```bash
loom tracker-feed init --org acme --host record.example --database team_feed \
  --writer-user feed_writer
```

初期化により、`github_issues`、`github_prs`、`edges` が definer ビューとして作成されます。リーダーにはこれらのビューへのアクセスのみを付与します。

```sql
GRANT SELECT ON team_feed.github_issues TO feed_reader;
GRANT SELECT ON team_feed.github_prs TO feed_reader;
GRANT SELECT ON team_feed.edges TO feed_reader;
```

`feed_reader` を使い、ビューモードで[トラッカーソースを登録](record-contract.md#adding-a-tracker-source)します。各リポジトリをソースの名前空間の許可リストに含め、非公開のレコードには制限付き名前空間を使用します。デプロイメントの `LOOM_TRACKER_ORG` にも同じ組織を設定します。ソース登録では、従来どおりホスト、認証情報、コントラクトの検証が行われます。HTTP とプライベートアドレスには開発用のプライベートホスト設定が必要で、本番の接続には HTTPS を使用します。

<a id="configure-and-schedule-a-repository"></a>

## リポジトリの設定とスケジュール

コピー対象のリポジトリに限定した、読み取り専用のきめ細かいトークンまたは App インストールトークンを作成します。必要な権限は、Metadata、Issues、Pull requests の読み取りです。`--read-only-token` は、権限についてオペレーターが行う表明です。CLI は、リクエストの成功やトークンのプレフィックスから、書き込み権限がないことを保証できません。

このトークンをシークレット機能を通じて `LOOM_TRACKER_FEED_GITHUB_TOKEN` として渡し、許可リストにある 1 つのリポジトリを設定します。

```bash
loom tracker-feed configure --org acme --source widgets --repo acme/widgets \
  --writer-user feed_writer --read-only-token --max-pages 5
loom tracker-feed sync --org acme --source widgets --repo acme/widgets \
  --writer-user feed_writer
```

設定では、トークンはチームのフィードデータベース内で暗号化して保管されます。同期にはライターのパスワードとデプロイメントのシークレットルートが必要ですが、トークンの環境変数は不要です。コピーされたリポジトリの識別子は、登録済みソースの許可リストでの表記を保持するため、API の大文字小文字が異なっても名前空間フィルターは一致します。App インストールトークンは[1 時間で失効します](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation)。外部のシークレット処理が、失効前にインストールトークンを更新して `configure` を再実行する必要があります。フィードはインストールトークンの発行も更新も行いません。別の認証情報用の変数を選ぶには、`--writer-password-env` または `--token-env` を使用します。ルートシークレットのローテーション中は、メタデータの書き込みが成功すると、`LOOM_JWT_SECRET_PREVIOUS` で開封された認証情報が再び暗号化されます。以前のルートを廃止する前に、正常なターンを 1 回完了させてください。

`sync` は 5 分ごと、またはそれより高い頻度でスケジュールします。可視性の観測結果は 15 分で失効するため、チェックが成功しなくなるとリポジトリは非表示になります。各ジョブの制限時間は 120 秒で、ストリームごとに最大 `--max-pages` ページを読み取ります。1 ページあたり 100 件、ページ数の予算は 2〜100 です。最後のページのタイムスタンプ境界は再生されます。最小値は、進行の起点となる直前のページを残すためのものです。容量エラーが発生してもカーソルは変わりません。再試行する前に、ページ数の予算の上限を引き上げるか、リポジトリのバックログを短くしてください。標準出力の件数は、再生された行を含む処理済みの行数を示します。

<a id="visibility-and-erasure"></a>

## 可視性と消去

すべてのストリームで、リポジトリの不変の識別子と現在の可視性を検証します。チェックに失敗すると、そのビューは非表示になります。各アイテムとコメントは、観測時のプライバシー分類をコンテンツとともに保持します。ビューでは、リポジトリ、アイテム、保存済みコメントの中で最も制限の厳しい分類が使用されます。これらのテーブルがレプリカに異なる時点で到達した場合も同様です。これは有効期限が限られたポーリングによる観測であり、権限の即時ミラーではありません。通常のトラッカーのプライバシー検証と直接付与の検証は、引き続き適用されます。

リポジトリが公開されると、設定または同期によってカーソルがリセットされ、スケジュールされたターンが同じページ数の予算内ですべてのストリームを再生します。アイテムは、その行と保存済みコメントが公開の観測のもとで更新された後に公開されます。上流で削除された行は、保持されているコピーの消去が必要です。再生ではそれらを更新できません。

本文には、アイテムは最大 32,768 文字、最後に更新された 64 件までのコメントが含まれ、コメントは 1 件あたり最大 2,048 文字です。レビュー依頼とテストの証跡は、ソースがその証跡を提供しない場合、不明のままです。ビューの `updated_at` は、レコード内でのアイテムとコメントの公開を追跡するため、アイテムより後にインポートされたコメントも、増分のトラッカー読み取りで引き続き参照できます。

増分リストは削除を報告しません。コピーされたコンテンツを消去するには、スケジュールされているすべてのライターを停止し、対象の raw 行、コメント、リポジトリ設定またはカーソルを消去し、再開する前にそのリポジトリを除外します。Loom の名前空間を削除すると Loom の派生データは削除されますが、チームのデータベースやバックアップは消去されません。初期化により、レコードは 1 つのデプロイメントと組織に紐付けられます。別のデプロイメントには別のレコードを使用してください。

カーソルの境界、並行性、所有権については、[フィードのアーキテクチャ](https://github.com/ClickHouse/loom/blob/main/docs/architecture/github-feed.md)を参照してください。
