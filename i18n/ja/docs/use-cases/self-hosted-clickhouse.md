<!-- i18n: source=docs/use-cases/self-hosted-clickhouse.md blob=347e84ca2fb7bc0a85662a875e99064979582695 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/self-hosted-clickhouse.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/self-hosted-clickhouse.md), which is authoritative.

<a id="self-hosted-agent-memory-on-clickhouse"></a>

# ClickHouse 上のセルフホスト型エージェントメモリ

Loom は自社のインフラストラクチャ上で、Docker または Helm チャートを使った Kubernetes で動作し、エージェントのメモリ、ドキュメント、会話、コードインデックスを ClickHouse のテーブルとして保存します。デプロイ時には、ライブのメモリとナレッジの状態に Postgres を初回起動時に選択できます。データは自分のストアにのみ保存され（設定したホスト型モデルプロバイダーとトレースシンクは、処理する内容を受け取ります）、エージェントが知っていることは SQL でクエリできます。

<a id="what-you-get"></a>

## 提供される機能

- **Docker または Kubernetes。** ワンライナーのインストーラーでローカルスタックを起動できます。Helm チャートは、1 つの URL の背後で N 個の同一レプリカを実行します。[デプロイ](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md) ·
  [Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md) · [N 個のレプリカの実行](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md)
- **ClickHouse OSS または ClickHouse Cloud。** [ClickHouse の選択](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#choosing-the-clickhouse) ·
  [ClickHouse Cloud と Managed Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/clickhouse-cloud.md)
- **ベクトル、テキスト、監査を 1 つのストアに。** デフォルトの ClickHouse オーソリティでは、ベクトル検索、全文検索、グラフ検索、フィルター、監査、消去がすべて 1 つのデータベース内の SQL で行えます。
  [ストレージオーソリティ](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-authority.md)
- **マルチユーザー。** 組織、名前空間、ロール、トークン、シングルサインオンに対応します。
  [ID と名前空間](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md) ·
  [シングルサインオン](https://github.com/ClickHouse/loom/blob/main/docs/operations/sso.md)
- **ローカルモデル。** リリース版のスタックは、同梱のローカルモデルを使って CPU 上で埋め込みを行います。また、埋め込みプロバイダーは、自分で運用する OpenAI 互換のエンドポイントを受け付けます。
  [モデルと API キー](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)

<a id="measured"></a>

## 計測結果

キーあたり 2.4 バージョンのメモリと 3072 次元の埋め込み（1 行あたり 2 つのベクトル）を使った合成負荷では、ClickHouse 上でキーあたり約 13.6 KB でした。そのほとんどは埋め込みの行が占めています。より小さな埋め込みモデルなら使用量は減り、行数の少ないパートは、マージされるまで 1 行あたりのサイズがやや大きくなります。
[大規模時のメモリ](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#memory-at-scale)

<a id="start"></a>

## はじめに

[クイックスタート](../getting-started/quickstart.md) · [前提条件](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#prerequisites) ·
[アップグレード](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md) · [バックアップとリカバリ](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)
