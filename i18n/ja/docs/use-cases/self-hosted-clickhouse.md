<!-- i18n: source=docs/use-cases/self-hosted-clickhouse.md blob=347e84ca2fb7bc0a85662a875e99064979582695 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/self-hosted-clickhouse.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/self-hosted-clickhouse.md), which is authoritative.

<a id="self-hosted-agent-memory-on-clickhouse"></a>

# ClickHouse 上のセルフホスト型エージェントメモリ

Loom は自社のインフラ上で動作します。Docker を使うか、Helm チャートを使って Kubernetes 上で実行でき、エージェントのメモリ、ドキュメント、会話、コードインデックスを ClickHouse のテーブルとして保存します。デプロイでは、初回起動時にライブのメモリとナレッジの状態に Postgres を選択することもできます。データは自社のストアにのみ保存されます（設定したホスト型モデルプロバイダーとトレースシンクは、処理対象のデータを受け取ります）。また、エージェントが把握している内容は SQL で照会できます。

<a id="what-you-get"></a>

## 提供される機能

- **Docker または Kubernetes。** ワンラインのインストーラーがローカルスタックを起動します。Helm チャートは、1 つの URL の背後で N 個の同一レプリカを実行します。[デプロイ](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md) ·
  [Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md) · [N 個のレプリカの実行](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md)
- **ClickHouse OSS または ClickHouse Cloud。** [ClickHouse の選択](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#choosing-the-clickhouse) ·
  [ClickHouse Cloud と Managed Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/clickhouse-cloud.md)
- **ベクトル、テキスト、監査を 1 つのストアに。** デフォルトの ClickHouse authority では、ベクトル検索、全文検索、グラフ検索、フィルター、監査、消去がすべて 1 つのデータベース内の SQL で実行されます。
  [ストレージ authority](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-authority.md)
- **マルチユーザー。** 組織、名前空間、ロール、トークン、シングルサインオンに対応します。
  [ID と名前空間](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md) ·
  [シングルサインオン](https://github.com/ClickHouse/loom/blob/main/docs/operations/sso.md)
- **ローカルモデル。** リリース済みのスタックは、同梱のローカルモデルを使って CPU 上で埋め込みを生成します。また、埋め込みプロバイダーは、ユーザーが運用する OpenAI 互換のエンドポイントも受け付けます。
  [モデルと API キー](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)

<a id="measured"></a>

## 実測値

1 キーあたり 2.4 バージョンのメモリと 3072 次元の埋め込み（1 行あたり 2 本のベクトル）を使った合成負荷では、ClickHouse 上で 1 キーあたり約 13.6 KB を要しました。そのほぼすべてが埋め込みの行です。より小さい埋め込みモデルを使えば容量は少なくなります。また、行数の少ないパートは、マージされるまで 1 行あたりの容量がやや大きくなります。
[Memory at scale](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#memory-at-scale)

<a id="start"></a>

## はじめに

[クイックスタート](../getting-started/quickstart.md) · [前提条件](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#prerequisites) ·
[アップグレード](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md) · [バックアップとリカバリ](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)
