<!-- i18n: source=docs/use-cases/self-hosted-clickhouse.md blob=347e84ca2fb7bc0a85662a875e99064979582695 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/self-hosted-clickhouse.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/self-hosted-clickhouse.md), which is authoritative.

<a id="self-hosted-agent-memory-on-clickhouse"></a>

# ClickHouse 上のセルフホスト型エージェントメモリ

Loom は自社のインフラストラクチャ上で、Docker または Helm チャートを使った Kubernetes で動作し、エージェントのメモリ、ドキュメント、会話、コードインデックスを ClickHouse のテーブルとして保存します。デプロイ時に、初回起動でライブメモリとナレッジの状態に Postgres を選択することもできます。データは自社のストアにのみ保存され（設定したホスト型モデルプロバイダーとトレースシンクは、処理対象のデータを受け取ります）、エージェントが何を知っているかは SQL でクエリできます。

<a id="what-you-get"></a>

## 提供される機能

- **Docker または Kubernetes。** ワンラインのインストーラーでローカルスタックが起動します。Helm チャートは、1 つの URL の背後で N 個の同一レプリカを実行します。[Deployment](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md) ·
  [Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md) · [Running N replicas](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md)
- **ClickHouse OSS または ClickHouse Cloud。** [Choosing the ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#choosing-the-clickhouse) ·
  [ClickHouse Cloud and Managed Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/clickhouse-cloud.md)
- **ベクトル、テキスト、監査を 1 つのストアに。** デフォルトの ClickHouse オーソリティでは、ベクトル検索、全文検索、グラフ検索、フィルター、監査、消去がすべて 1 つのデータベース内の SQL で行われます。
  [Storage authority](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-authority.md)
- **マルチユーザー。** 組織、名前空間、ロール、トークン、シングルサインオンに対応します。
  [Identities and namespaces](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md) ·
  [Single sign-on](https://github.com/ClickHouse/loom/blob/main/docs/operations/sso.md)
- **ローカルモデル。** リリース版のスタックは、同梱のローカルモデルを使って CPU 上で埋め込みを行います。また、埋め込みプロバイダーは、ユーザーが運用する OpenAI 互換のエンドポイントも利用できます。
  [Models and API keys](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)

<a id="measured"></a>

## 計測結果

キーあたり 2.4 バージョン、3072 次元の埋め込み（1 行あたりベクトル 2 本）という合成負荷でメモリをロードしたところ、ClickHouse ではキーあたり約 13.6 KB を占め、そのほとんどが埋め込みの行でした。より小さい埋め込みモデルを使えば容量は減ります。また、行数の少ないパートは、マージされるまでは 1 行あたりの容量がやや大きくなります。
[Memory at scale](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#memory-at-scale)

<a id="start"></a>

## 開始

[クイックスタート](../getting-started/quickstart.md) · [前提条件](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#prerequisites) · [アップグレード](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md) · [バックアップとリカバリ](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)
