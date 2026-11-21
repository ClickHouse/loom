<!-- i18n: source=docs/guides/teaching.md blob=5ff57fa6e6aa684faaf742b9ce4b6c698eda6682 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/guides/teaching.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/teaching.md), which is authoritative.

<a id="teaching-with-loom"></a>

# Loom で教える

Loom は、AI エージェント、情報検索、データベース、ソフトウェアエンジニアリングの講座の実習環境として利用できます。各学生は、Docker を使うノート PC 上、または手元に何もインストールしなくてよい GitHub codespace 上で、自分専用のシングルユーザースタックを実行します。各実習では、その作業を扱うページへのリンクを示します。

<a id="setup"></a>

## セットアップ

| 受講者の環境                      | 最初に読むページ                                                                                  |
| ------------------------------ | ------------------------------------------------------------------------------------------- |
| GitHub アカウントとブラウザ         | [GitHub Codespaces で試す](../getting-started/quickstart.md#try-it-in-github-codespaces) |
| 自分のマシン上の Docker           | [クイックスタート：高速パス](../getting-started/quickstart.md#fast-path-released-image-no-clone) |

codespace は、実行中およびディスクが保持されている間、受講者の Codespaces クォータを消費します。また、保持期間を過ぎても使われなかった codespace は、そのデータごと GitHub によって削除されます（[課金](https://docs.github.com/en/billing/concepts/product-billing/github-codespaces)、[自動削除](https://docs.github.com/en/codespaces/setting-your-user-preferences/configuring-automatic-deletion-of-your-codespaces)）。dev container の `hostRequirements` が、動作する最小のマシンを定めています（[`.devcontainer/devcontainer.json`](https://github.com/ClickHouse/loom/blob/main/.devcontainer/devcontainer.json)）。

標準のインストールに API キーは不要です。同梱のローカルモデルで埋め込みを行うため、検索はオフラインで動作し、書き込みの判定で LLM を呼び出すこともありません。抽出、要約、生成された回答（`memory.ask`、`knowledge.synthesize`）には実際の LLM が必要です（[モデルと API キー](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)）。以下の演習で使うのは、書き込み、読み取り、検索のみです。

Lab 1 と、Lab 2 の SDK を使う方法では、ノート PC または codespace のターミナルで、仮想環境に Python SDK を用意する必要があります。Debian または Ubuntu（codespace と WSL を含む）では、以下のコマンドの前に `sudo apt-get update && sudo apt-get install -y python3-venv` を実行してください。SDK には Python 3.11 以降が必要です。インストール済みのバージョンは `python3 --version` で確認できます。Python 3.11 以降がない受講者は codespace を使えます。この手順で十分に新しい Python がインストールされます。

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install clickhouse-loom
```

演習中の `docker compose` コマンドは同じターミナルで実行し、スタックのコンテナを操作します。インストールディレクトリは `<dir>` と表記しており、ノート PC では `~/.loom`、codespace では `/workspaces/.loom` です。

<a id="hand-out-the-labs-as-an-assignment"></a>

## ラボを課題として配布する

[コーススターター](https://github.com/ClickHouse/loom/blob/main/examples/course-starter/README.md)は、学生ごとに用意するリポジトリです。Lab 1 と Lab 2 のコード、Lab 4 のクエリ（空欄には `TODO` が付いています）、筆記課題用の解答ページ、最新リリースを Python SDK と Claude Code とともにコードスペースにインストールする dev container、採点ワークフローが含まれます。これをあなたのアカウントまたは学校の組織の下に新しいリポジトリとしてコピーし、そのリポジトリをテンプレートに設定してください（`curl`、`tar`、`git`、およびサインイン済みの [GitHub CLI](https://cli.github.com/) が必要です）。

```bash
mkdir loom-labs && cd loom-labs
curl -fsSL https://github.com/ClickHouse/loom/archive/refs/heads/main.tar.gz \
  | tar -xz --strip-components=3 loom-main/examples/course-starter
git init -b main && git add . && git commit -m "Loom labs"
gh repo create <owner>/loom-labs --public --source . --push
gh repo edit <owner>/loom-labs --template
```

push すると、リポジトリがテンプレートになる前に採点ワークフローが一度実行されます。この実行は設計上失敗します。各チェックの行に、スターターのうちまだ書かれていない部分が示されます。コピーは `main` 上のスターターで、`main` に対してテストされていますが、実行のたびに最新リリースがインストールされます。したがって、これ以外の理由で失敗した場合は両者に差異があるということなので、次のリリースの後にスターターをもう一度コピーしてください。テンプレートへの以降の push は採点されません。

その後、各学生はそのテンプレートから作成したリポジトリで作業します。テンプレートのページの **Use this template** か、課題配布に使っているツールで作成し、コードスペースで開くか、ノート PC にクローンします。学生がテンプレートを使うには、テンプレートへの読み取りアクセスが必要です。公開テンプレートには解答が含まれていません。非公開テンプレートは、その組織のメンバーか、あなたが追加したコラボレーターであれば使えます。

`python grading/grade.py` は、学生の稼働中のスタックに対してラボのコードを実行し、そのコードが書き込んだ内容と返した内容を確認します。どのように到達したかは確認しないので、コードも読んでください。`Grade` ワークフローは、push のたびに同じチェックを GitHub Actions ランナー上の新しいスタックに対して実行し、チェックの表を実行のサマリーに書き出します。チェックが一つでも失敗している間は、実行も失敗します。各チェックは手を加えていないスターターでは失敗し、API キーは不要です。Lab 3 と解答ページは、課題が説明を求めているため、手動で採点します。

採点の実行には約 2 分かかり、そのほとんどはスタックの起動に費やされます。非公開リポジトリでは、この時間はリポジトリを所有するアカウントまたは組織の Actions のクォータに計上されます（[Actions の課金](https://docs.github.com/en/billing/concepts/product-billing/github-actions)）。コードスペースの費用は[セットアップ](#setup)に記載のとおりです。

<a id="lab-1-an-agent-that-remembers"></a>

## ラボ 1: 記憶するエージェント

Python SDK で小さなエージェントを作ります。ID、保存して読み戻すファクト、セッション、実行（run）を扱います。手順は [最初のエージェント](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md) に従ってください。

演習：

- どの保存済みファクトにも答えがない内容を `memory.search` で検索し、返信に含まれる `abstain_code` について説明します（[検索と質問](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)）。
- ファクトを保存し、次に近いが異なる値を別のキーで保存して、書き込みの判定結果を読みます（[書き込みの判定方法](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md#how-a-write-is-judged)）。
- ファクトを改訂し、`memory.history` でそのバージョンを読みます（[履歴と来歴](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md#versions-memoryhistory)）。

<a id="lab-2-document-search-with-citations"></a>

## Lab 2: 引用付きドキュメント検索

`loom demo` でいくつかのドキュメントを取り込みます。標準スタック（偽の LLM）では、回答の代わりに、質問に対する `knowledge.search` の上位ヒットが出力されます（[ドキュメントについて質問する](../getting-started/quickstart.md#optional-ask-a-question-about-a-document)）。`--file` のパスはコンテナ内のパスで、コンテナには `/app/docs/` の下にこれらのドキュメントが入っています。自分のファイル（ここではカレントディレクトリの `notes.md`）は、先にコンテナへコピーします。`loom demo` はドキュメントをパスで命名するため、ファイルを識別できる名前を付けてください。同じパスに別のファイルをコピーすると、最初のファイルが置き換えられ、そのファイルの次のリビジョンになります。

```bash
docker compose --project-directory <dir> exec loom loom demo --file /app/docs/getting-started/quickstart.md --question "How do I get a token?"
docker compose --project-directory <dir> exec -T loom sh -c 'cat > /tmp/notes.md' < ./notes.md
docker compose --project-directory <dir> exec loom loom demo --file /tmp/notes.md --question "<a question the file answers>"
```

その後、HTTP（シングルユーザースタックでは `Authorization` ヘッダーなし）または SDK で、`knowledge.search` を使って名前空間のドキュメントを検索します（[ナレッジドキュメント](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md)）。SDK の `knowledge.ingest_file` は、ローカルファイルをコンテナにコピーせずに公開します。コースのスターターの `labs/lab2.py` がこれを使います。

演習：

- 10 個の質問それぞれについて、答えとなるドキュメントとパッセージにラベルを付け、`knowledge.search` の recall@k を測定します（[ワークロードを評価する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md#memory-and-rag)）。
- ドキュメントでは答えられない質問を追加します。`knowledge.search` はそれらの質問にもヒットを返すため、答えられる質問と答えられない質問とで、ヒットの中で最も高い `similarity`（1 からコサイン距離を引いた値）を比較し、両者を分けるカットオフを選びます。ヒットの `score` はデフォルトのハイブリッドモードでは融合されたランクであり、両者を分けられません。

<a id="lab-3-a-code-index"></a>

## Lab 3: コードインデックス

スタックでリポジトリをインデックス化します
（[リポジトリをインデックス化する](../getting-started/quickstart.md#optional-index-a-repository-on-the-released-stack)）。
その後、`POST /v1/code.<op>` を使い、HTTP 経由で、シンボルがどこで定義されているか、誰が呼び出しているか、変更が何に影響するかを尋ねます（[コードインデックスが答えること](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#what-it-answers)）。シングルユーザースタックは `code.*` の呼び出しに値を補完しないため、各ボディに `"org": "dev"` とリポジトリの `"namespace"`（クイックスタートの例では `code-myrepo`）を指定します。

コーディングエージェントを接続すれば、同じ質問をエージェントからも行えます（[コーディングエージェントを接続する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)）。エージェントはそれ自身のプロバイダーのモデルで動作するため、そのプロバイダーへのサインインまたは API キーが必要です。スタックはこれらを提供しません。

演習：

- 関数を 5 つ選び、`code.callers` の結果を `grep` の結果と比較して、それぞれの違いを説明してください（[回答の読み方](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#reading-an-answer)）。
- インデックス化したリポジトリのクローンで関数を変更し、その diff を `diff` として `code.impact` に送信します（[エージェントでの使い方](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#how-an-agent-uses-it)）。このコマンドには `jq` が必要です（Debian または Ubuntu では `sudo apt-get install -y jq`）。

  ```bash
  git diff | jq -Rs '{org: "dev", namespace: "code-myrepo", diff: .}' \
    | curl -s -X POST http://localhost:7777/v1/code.impact -H 'Content-Type: application/json' -d @-
  ```

<a id="lab-4-memory-as-a-database"></a>

## Lab 4: データベースとしてのメモリ

Loom はデータを ClickHouse のテーブルの行として保持し、読み取り専用の ClickHouse ユーザーはビューを通じて名前空間を読み取ります（[SQL サーフェス](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)）。スタックの名前空間 `local` 用にユーザーを作成し（最初のコマンドでパスワードが表示されます）、そのユーザーでクエリを実行します。

```bash
docker compose --project-directory <dir> exec loom loom sql-access grant --user student1 --org dev --namespaces local
docker compose --project-directory <dir> exec clickhouse clickhouse-client --user student1 --password <password>
```

演習：

- `loom.sql_v1_memories` に対して、直近 1 時間に各エージェントが作成したファクトの数を数えるクエリを書いてください（リビジョンが保持する `created_at` を使います）。
- `memory.search` の結果と、同じ行に対する SQL クエリの結果を比較し、ランキングが何を加えるかを説明してください。

<a id="which-labs-fit-which-course"></a>

## コースごとに適したラボ

| コース | ラボ | 次に読むもの |
| --------------------- | ------- | --------------------------------------------------------------------------------------------------------- |
| AI エージェント | 1, 2, 3 | [Loom をエージェントに統合する](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md)、[メモリ](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md) |
| 情報検索 | 1, 2 | [検索と質問](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)、[Loom を評価する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md) |
| データベース | 1, 4 | [ストレージオーソリティ](../architecture/storage-authority.md)、[SQL サーフェス](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md) |
| ソフトウェアエンジニアリング | 3 | [コードインデックス](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)、[PR 履歴](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) |
