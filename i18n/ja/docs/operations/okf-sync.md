<!-- i18n: source=docs/operations/okf-sync.md blob=d6e5ec3b4a727da4861241c4b6c6b3efe03f98f2 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/operations/okf-sync.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/okf-sync.md), which is authoritative.

<a id="copying-a-namespace-to-another-loom"></a>

# 名前空間を別の Loom にコピーする

プライベートネットワーク内のジョブが、スケジュールに従って 1 つの名前空間をプライベートな Loom からパブリックな Loom にコピーします。`loom export-okf` がプライベートな Loom から名前空間を読み取り、`loom import-okf --prune` がパブリックな Loom に書き込みます。パブリック側には、プライベートな Loom へのルートもトークンも渡りません。`deploy/okf-sync/cronjob.yaml` は Kubernetes の例で、15 分ごとに実行される CronJob、その ServiceAccount、および NetworkPolicy を含みます。

```bash
LOOM_API_KEY="$PRIVATE_VIEWER" loom export-okf --server-url "$PRIVATE_URL" \
  --namespace shared-lessons --no-evidence --out /tmp/bundle
LOOM_API_KEY="$PUBLIC_WRITER" loom import-okf /tmp/bundle --server-url "$PUBLIC_URL" \
  --namespace shared-lessons --agent okf-sync --prune
```

<a id="what-a-run-writes"></a>

## 実行時の書き込み内容

- **変更がなければ何も書き込みません。** インポートは、まず自身の identity 配下の有効な行を読み取り、値、型、タグ、信頼度、`valid_at`、`valid_to`、`source_trace`、`source_class`（宣言されたクラス、なければ `imported`）が同じ行を持つコンセプトをスキップします。バンドルにエンティティが含まれる場合は、エンティティも比較します。`memory.set` 自身が追加するタグ（`sensitivity:`、`attribute:`）は差分に含まれません。すでに有効なエッジもスキップします。バンドル内の複数のコンセプトが同じキーを共有している場合（異なる書き込み元の行）、最後に更新されたものが書き込まれます。同一の行を再送すると強化（reinforcement）として数えられ、実行のたびにバージョン行と監査行が増え、ランキングのブーストも高くなります。`sources:` を持つコンセプトは例外で、実行のたびに書き込まれます。インポートが行う読み取りでは、リンク先のソースが返されないためです。ターゲットが別のキーの行にマージするコンセプトも同様です。実行のたびに再送され、その行を強化し、最終行ではマージ済みとして数えられます。
- **新規または変更されたコンセプトごとに `memory.set` を 1 回実行します。** 他の書き込みと同じく、競合、ブラックリスト、監査のチェックを通り、トークンが actor になります。ソースが `derived` または `external` とマークした行はそのクラスを保持し、それ以外の行はすべて `imported` として保存されます。これは `okf:` トレースからサーバーが導き出します。両側で org と名前空間が同じ場合、コピーに `origin:` タグは付きません。
- **プルーニングされた行ごとに `memory.delete_version` を 1 回実行します。** `--prune` が削除するのは、インポートの `--agent` 配下にあり、そのトークンで書き込まれ、`source_trace` が `okf:` で始まり、キーがバンドルにもう存在しない行だけです。名前空間内で他者が書き込んだ行は残ります。名前空間にインポートするバンドルごとに、固有の `--agent` を指定してください（指定がない場合、`--prune` は拒否されます）。削除はトゥームストーンであり、`memory.restore` で 14 日間の期間内なら行を元に戻せます。

最終行にはカウントが報告されます。
`500 rows from the bundle into acme/shared-lessons: 0 written, 500 unchanged, 0 failed …,
0 pruned`。
終了コード 1 は、ターゲットを読み取れなかった、または書き込み、リンク、削除、エビデンスのバッチ、`--verify` のいずれかが失敗したことを意味します。終了コード 2 は、リクエストを送る前にインポートが拒否されたことを意味します。lint の指摘、プルーニングのガード、ディレクトリではないパス、または `--agent`、トークン、org の欠落が該当します。

<a id="guards"></a>

## ガード

- `--prune` は、コンセプトを含まないバンドルを拒否します。また、コンセプトファイルのハッシュが、エクスポート時に `index.md` へ書き込まれた `loom.corpus_digest` と一致しないバンドルも拒否します。編集されたバンドルや、古いバンドルが残るディレクトリにエクスポートされたバンドルが、削除を引き起こすことはありません。`index.md` に `loom.complete: true` がないバンドルは書き込まれますが、プルーニングはされず、最終行にその旨が表示されます。エクスポートは、`--limit` で停止した場合、`--include-tombstoned` を付けて実行した場合（このバンドルには削除済みの行が残ります）、または読み取り中にソースが変更された場合（ページング中の書き込み、または全体を読み取る前の行の削除）にマーカーを付けず、その理由を表示します。古いリリースが書き出したバンドルにはマーカーがありません。
- エクスポートは、ソース側で置き換えられた行と重複した行を `status: deprecated` として含めます。そのため、ソースが別のキーに統合したキーも、プルーニングされずにコピー先に残ります。
- エクスポートはソースをオフセットでページングするため、一覧取得中にソースへ書き込みがあると、ページがずれて有効な行を取りこぼすことがあります。ページ取得の後、エクスポートは `memory.keys` を読み込みます。これは、すべての有効な行の ID とキーを 1 つのステートメントで返すもので（ある時点の名前空間）、それらの行がすべて一覧に含まれていた場合にのみ、一覧を完全とみなします。その時点より後に書き込まれた行は後の変更であり、次回の実行が引き継ぎます。一覧取得は合計で最大 3 回行われ、それでも完全にならなければ `loom.complete` は付きません。その場合、その実行はコピーのみでプルーニングは行わず、後の、より書き込みの少ない実行でプルーニングします。`memory.keys` に応答できない古いソース、または有効な行が 100,000 を超えるソースは、完全とはみなされません。
- 各削除の前に、その行を自身の ID で再度読み取り、次のすべてを満たす場合にのみ続行します。その行が自身の `--agent` の下にあること、自身のトークンで書き込まれたこと（エージェント名だけでは呼び出し側が選んだものにすぎません）、インポートの一覧が `okf:` トレース付きで見たバージョンのままであること、`memory.lock` がないことです。それ以外は保持としてカウントされ、その時点で存在しない行や別のエージェントの下にある行はカウントされません。削除には `memory.delete_version` を使い、その正確な ID の下にある該当バージョンだけを削除します。他のエージェントの行は削除しません。削除が実行された時点で進行中の、そのキーに対する `memory.set` または `memory.bulk_set` は、その行を保持します。例外については[メモリアーキテクチャ](https://github.com/ClickHouse/loom/blob/main/docs/architecture/memory-architecture.md#lifecycle)を参照してください。
- `memory.delete_version` を知らない古いターゲットレプリカは 404 を返し、何も削除しません。プルーニングはこの失敗をカウントし、インポートは終了コード 1 で終了します。どちらの Loom もローリングアップグレードで一時停止する必要はありません。
- インポートは、エクスポートが終了コード 0 で終了した場合（`sh -e`）にのみ実行されます。また、各実行はバンドルを新しい `emptyDir` に書き込むため、以前の実行が残したバンドルがインポートされることはありません。
- `--dry-run` は、同じチェックを行った上で、書き込み・削除される内容を一覧表示し、何も書き込みません。
- `concurrencyPolicy: Forbid` により、2 つの実行が並行して動作することはありません。

1 回の実行でプルーニングできる行数に上限はありません。意図的に大半の行を失ったソースの名前空間は、オペレーターの介在なしに次回の実行で反映されます。すべての行を失った場合は反映されません。インポートが空のバンドルを拒否するため、ジョブは毎回失敗します。CronJob を停止し（`kubectl patch cronjob okf-sync -p '{"spec":{"suspend":true}}'`）、公開側の Loom にあるコピーを `memory.delete` で削除してください。内容自体を消す必要がある場合は、消去（erasure）を通じて削除します。

誤ってプルーニングされたキーは、そのキーを含むバンドルを持つ次回の実行で、新しい行として再び書き込まれます。このとき `memory_id` は変わります。元の行を復元するには `memory.restore` を使います。

<a id="what-a-run-costs"></a>

## 実行のコスト

変更対象が見つからない実行は、読み取りだけを送信します。`sensitivity: high` の行をキーで読み取るたびに監査（`memory.sensitive_read`）が記録されるため、インポートの ID の下に high の行があると、何も変更しない実行でも、その行をキーで読み取る側ごとに、high の行 1 件につき監査行が 1 件追加されます。N 行の名前空間では次のとおりです。

| 側                    | リクエスト                                                                                                                                                                                                                                                                                                                                                  |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| エクスポート、プライベート Loom | 100 行ごとの `memory.list` ページ（`LOOM_MEMORY_LIST_VIEWER_LIMIT` が viewer トークンのページ上限）、`memory.keys` 1 回、ビルドコミットを取得する `GET /v1/health` 1 回、（agent、user、session）それぞれ 500 行ごとに `memory.bulk_get` 1 回、型付きエッジ 2,000 件ごとに `memory.export_evidence` 1 ページ |
| インポート、パブリック Loom | 1,000 行ごとの `memory.list` ページ、500 行ごとに `memory.bulk_get` 1 回、エッジのページの再取得。続いて、変更された行ごとに `memory.set` 1 回、新しいエッジごとに `memory.link` 1 回、プルーン候補（`okf:` トレースを持ち、キーがバンドルから消えた行。残すかどうかは問わない）ごとに `memory.bulk_get` 1 回、プルーンされた行ごとに `memory.delete_version` 1 回 |

writer が 1 つの 501 行の名前空間では、合計 15 リクエストです（エクスポート側は `memory.list` 6 回、`memory.keys` 1 回、health 読み取り 1 回、`memory.bulk_get` 2 回、エッジページ 1 回。インポート側は 1、2、1）。1 つの ID の下で 50,000 行ある場合、1 回の実行は約 750 リクエストで、15 分間隔のスケジュールでは 1 秒あたり 1 件未満です。フックが書き込むように多数のセッションに分散した名前空間では、代わりにセッションごとに `memory.bulk_get` が 1 回必要です。また、ページング中にソースが変更された場合、エクスポートはソースを合計で最大 3 回一覧取得します。

<a id="tokens"></a>

## トークン

ジョブは 2 つのトークンを持ち、それぞれ 1 つの名前空間にスコープされます。

```bash
# On the private Loom: read the namespace, nothing else.
loom mint-token --subject okf-sync --org <org> --namespace-role shared-lessons=viewer \
  --ttl-days 90 --save ./private-viewer.jwt
# On the public Loom: write the namespace, nothing else.
loom mint-token --subject okf-sync --org <org> --namespace-role shared-lessons=writer \
  --ttl-days 90 --save ./public-writer.jwt
```

CronJob が読み込む Secret に保存します。

```bash
kubectl create secret generic okf-sync \
  --from-file=private-viewer=./private-viewer.jwt \
  --from-file=public-writer=./public-writer.jwt
```

TTL が切れる前に、同じ `--subject` と同じ種類（デフォルトのサービスアカウント）で再発行し、Secret を置き換えてください。prune が削除するのは、そのトークンの actor が書き込んだ行のみです（サービスアカウントでは `process:<subject>`、ユーザートークンでは `human:<subject>`）。そのため、subject や種類が異なるトークンを使うと、以前のコピーはそのまま残ります（最後の行では保持として数えられます）。漏洩したトークンは `loom revoke-token` で無効化します。認証がオフのサーバーでは、どの行にも actor が付かないため、prune は何も削除せず、すべての候補を保持として数えます。

- 公開 Loom では、ジョブのトークンがその名前空間で唯一の書き込み権限を持ちます。読み取り側（公開 CI のボットなど）は `viewer` トークンを持ちます。
- 非公開 Loom では、その名前空間の書き込み権限を持つのは、そこに公開するユーザーとエージェントです。それを読む他のすべてのプリンシパルは `viewer` を持つため、書き込み側が公開するつもりのないものが名前空間に届くことはありません。

<a id="the-copied-namespace-is-published"></a>

## コピーされた名前空間は公開される

名前空間内のすべての内容は、1 回のスケジュール間隔以内に公開 Loom に反映されます。プライベート側でこの名前空間に書き込むことは公開と同じだと考えてください。機密情報は別の名前空間に置いてください。`sensitivity: high` が付いた行はプライベート側にとどまります（`--include-high` を指定しない限り、エクスポートはそれらを除外します）。行が `high` になった場合は、次の実行時に公開側から削除されます。ただし、それがエクスポートの対象となる最後の行だった場合は例外です。その場合、バンドルにコンセプトが含まれなくなるため、インポートはバンドルを拒否し、公開側のコピーは残ります。公開 Loom 上でこれらを削除するには `memory.delete` を使います。内容自体を消す必要がある場合は、消去（erasure）を使います。エクスポートは、`memory.bulk_get` から読み取る `sensitivity` によって、または `memory.set` と `memory.update` が付与する `sensitivity:high` タグによって、`high` の行を判別します。`memory.bulk_get` が `sensitivity` を返さないプライベート Loom に対しては、すべての行が手元に残ります（エクスポートはそれらを「no known sensitivity」として数えます）。そのためバンドルにコンセプトが含まれず、`--prune` はそれを拒否します。prune はトゥームストーンです。値は 14 日間復元でき、公開 Loom のバージョン履歴にも残ります。公開 Loom から消す必要がある内容は、[消去](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)の手順で処理してください。

<a id="network"></a>

## ネットワーク

NetworkPolicy は ingress を一切許可しません。egress は、DNS、プライベート Loom の web Pod への 7777 番ポート、およびパブリック Loom のアドレスへの 443 番ポートに限定されます。NetworkPolicy はホスト名ではなくアドレスで照合します。パブリック Loom のアドレスは `ipBlock` に列挙してください。アドレスが変わる場合は、CNI のホスト名ポリシーまたは egress プロキシを使用してください。

プライベートリリースで `networkPolicy.enabled` を `ingressFrom` とともに設定する場合は、ジョブの Pod（`app.kubernetes.io/name: okf-sync`）またはその名前空間を `ingressFrom` に追加してください。追加しないと、リリース自身のポリシーによってエクスポートが拒否されます。

<a id="related"></a>

## 関連項目

- [OKF バンドル](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/okf.md)：バンドル形式とインポートについて説明します。
- [Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md)：CronJob がポッドのセキュリティ設定を引き継ぐチャートです。
