# Benchmark results

This page lists only numbers that a committed file reproduces. Each result is one of three
kinds:

- a **measurement** of Loom under a real provider, recorded in a committed artifact;
- a **reference computation** the code produces on fixed input;
- a **scorer self-check** that proves a harness counts correctly.

Only measurements say anything about Loom's retrieval.

The [coverage matrix](./coverage.md) separates product surfaces with a published
measurement from those with only a harness or no public evidence yet.

## Measurements

Each result reports one metric. **Hit@k** asks whether at least one gold item appeared within
the first `k`; **recall@k** averages the fraction of all labelled gold items retrieved;
**all-gold@k** requires every gold item. F1 balances precision and recall, and MRR rewards
placing the first gold item near the top. Retrieval metrics and answer accuracy, or answer
scores from different readers or judges, are not comparable. Latency results describe one
server and ClickHouse deployment, not a capacity promise.

Each measurement names its artifact, the instrument it was recorded under and a
reproduction command. New committed results go in this section, one subsection each.

A benchmark name identifies the source corpus, not automatically the official task or
leaderboard protocol. Every derived task below says what Loom actually received and what
the scorer counted. “All” means the complete published split; “sample” or “slice” means
the stated selection. Answer scores are comparable only when reader and judge are held
constant; retrieval scores are not answer accuracy or issue-resolution rates.

### Published benchmark protocol index

Use this table to establish exactly what each published number measures before comparing
results. “Questions” are independent prompts scored against ground truth; “contexts” are
the conversations, documents or repository revisions searched to answer them. Follow the
benchmark link for models, category results, limitations, artifact paths and the exact
reproduction command.

| Benchmark                                                                             | Exact task                                                                                                                                                                                                                 | Corpus and evaluation size                                                                                                                                                                                                                                         | Loom path and search depth                                                                                                                                                                                                                                                                                     | Success definition or comparison                                                                                                                                                                                                                                                                                                                          |
| ------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [LoCoMo conversational QA](#locomo-conversational-qa)                                 | Answer questions about facts, events, relationships and time across long conversations; abstain on adversarial questions.                                                                                                  | All 10 conversations; 1,986 questions: 1,540 answerable and 446 adversarial.                                                                                                                                                                                       | Conversations ingested with `memory.set_from_messages`; `memory.ask`, or `memory.search` top 10 followed by fixed `gpt-4.1-mini`.                                                                                                                                                                              | `gpt-4.1`-judged answer accuracy for categories 1–4; correct abstention for category 5, which published LoCoMo comparisons normally exclude.                                                                                                                                                                                                              |
| [LongMemEval-S](#longmemeval-s)                                                       | Answer questions requiring information distributed across many earlier sessions and retrieve the supporting evidence.                                                                                                      | All 500 questions; about 47 sessions in each question's history.                                                                                                                                                                                                   | Histories ingested with `memory.set_from_messages`; `memory.ask`, or `memory.search` top 200 followed by `gpt-5`; supporting-session recall scored at 30.                                                                                                                                                      | Official LongMemEval judge; `memory.ask` is Loom's product flow, not a standard leaderboard configuration; the fixed-reader arm is reported separately.                                                                                                                                                                                                   |
| [MemBench](#membench)                                                                 | Answer multiple-choice questions about events the agent observed or participated in.                                                                                                                                       | Two scenarios; 140 questions per scenario, covering seven question types.                                                                                                                                                                                          | Scenario memories ingested, then answered with `memory.ask`.                                                                                                                                                                                                                                                   | Exact multiple-choice accuracy; no LLM judge or external baseline.                                                                                                                                                                                                                                                                                        |
| [MemoryAgentBench samples](#memoryagentbench)                                         | Answer fact questions, resolve conflicting facts, answer from a 197K-token context and learn labels supplied at test time.                                                                                                 | Selected slices: 15 contexts and 1,500 questions—retrieval 500, conflict 800, RULER 100 and test-time learning 100.                                                                                                                                                | Contexts chunked through `memory.set_from_messages`; generated answers produced by `memory.ask`.                                                                                                                                                                                                               | SubEM and token F1 score answers, not raw retrieval; test-time learning uses label presence. No external reader or LLM judge.                                                                                                                                                                                                                             |
| [MuSiQue-Ans development sample](#musique-ans)                                        | Retrieve multiple supporting paragraphs for a multi-step question, then answer from those passages.                                                                                                                        | Seed-0 sample of 200 questions from the development set.                                                                                                                                                                                                           | Each question's paragraphs written with `memory.set`; semantic `memory.search` top 10; fixed `gpt-4o-mini` reader.                                                                                                                                                                                             | Mean fraction of gold supporting paragraphs retrieved at 10; exact match and token F1 against gold answers; no LLM judge.                                                                                                                                                                                                                                 |
| [PerLTQA sample](#perltqa)                                                            | Answer personal long-term-memory questions about profiles, relationships, events and dialogues.                                                                                                                            | 193-question slice: five fictional characters, at most 10 questions per section.                                                                                                                                                                                   | Character histories ingested with `memory.set_from_messages`; answered by `memory.ask`.                                                                                                                                                                                                                        | `gpt-4o-mini`-judged answer accuracy against reference answers, with two additional judges reported.                                                                                                                                                                                                                                                      |
| [Tracker duplicate detection](#duplicate-check-on-public-clickhouse-issues)           | Given a new issue draft, find its recorded older duplicate while limiting duplicate verdicts on issues with no recorded duplicate/related link.                                                                            | 104 recorded duplicate pairs and 100 negative-labelled issues from ClickHouse's public tracker.                                                                                                                                                                    | `tracker.duplicate_check` receives title and body as of creation time; the draft's issue number and recorded links are withheld.                                                                                                                                                                               | Older linked issue ranked first and duplicate verdict correctness; a negative label means no recorded link, not proof that two issues are unrelated.                                                                                                                                                                                                      |
| [ClickHouse code intelligence](#code-intelligence-on-the-clickhouse-repository)       | Find definitions, callers and implementation files for authored questions; abstain on authored out-of-repository probes.                                                                                                   | 60 authored questions: 10 definitions, 4 caller sets, 36 conceptual searches and 10 out-of-repository probes.                                                                                                                                                      | Public ClickHouse repository index; `code.symbol`, `code.callers` and `code.search` with their normal result limits.                                                                                                                                                                                           | Exact first definition; F1 against expected callers; mean gold-file recall@5/@10, MRR and hit@5; abstention precision/recall.                                                                                                                                                                                                                             |
| [SWE-bench Verified sample: file localization](#swe-bench-verified-file-localization) | Given a GitHub issue description, rank the pre-existing repository files that its accepted patch later modified. This is retrieval, not patch generation or issue resolution.                                              | Stratified seed-7 sample of 200 eligible instances from the 500-instance dataset, covering all 12 repositories; eligibility requires at least one patch-modified file present at `base_commit`.                                                                    | Each repository indexed at the issue's `base_commit`; issue text truncated at 2,000 characters; `code.search` first 10 results and a second page to 25 units.                                                                                                                                                  | File hit@1, hit@5 and hit@10 (at least one gold file); all-gold@10; MRR; paired against BM25 over the same live files and over Python files only.                                                                                                                                                                                                         |
| [Memory versus full history](#with-and-without-loom)                                  | Answer the same long-conversation question from retrieved memories or from the complete conversation history.                                                                                                              | Paired seed-13 sample of 100 LongMemEval-S questions.                                                                                                                                                                                                              | Same `gpt-5` reader sees either `memory.search` top 200 or the full history.                                                                                                                                                                                                                                   | Official-judge accuracy and reader prompt tokens; memory construction is excluded from prompt-token totals and reported separately as break-even.                                                                                                                                                                                                         |
| [Coding agents on ClickHouse: with Loom vs without Loom](#with-and-without-loom)      | Answer a ClickHouse developer's question: explain a mechanism, list the callers a change must update, find the change behind a behaviour, trace a bug to its fix, find the tests that cover a change.                      | 48 answer-graded questions × 3 paired runs and 36 conceptual questions × 10 paired runs; checkouts without git history at the verified commit.                                                                                                                     | Same headless `claude-sonnet-5` in a stock session; baseline has Bash, Read, Grep and Glob, the Loom arm adds the Loom plugin and its MCP `coding` toolset.                                                                                                                                                    | Judged key points of a verified reference answer, right file in the three named; model cost, turns and wall time per run.                                                                                                                                                                                                                                 |
| [Definitions and callers versus grep](#with-and-without-loom)                         | Find a symbol's definition or all expected callers using Loom or repository text search.                                                                                                                                   | The same 10 definition and 4 caller questions from the ClickHouse code-intelligence set.                                                                                                                                                                           | Loom's code index versus `git grep` for the bare name and the qualified `Class::name`; grep output tokens estimated as characters divided by four.                                                                                                                                                             | Correct file ranked first, expected callers found and context tokens returned.                                                                                                                                                                                                                                                                            |
| [`code.ask` production cache](#with-and-without-loom)                                 | Measure how often repeated real questions reuse a cached answer and what one hit saves.                                                                                                                                    | 673 non-benchmark calls from one deployment over 30 days; 20 cache hits.                                                                                                                                                                                           | Normal `code.ask` traffic using the shipped cache guard and threshold.                                                                                                                                                                                                                                         | Observed hit rate, answer tokens avoided and latency; this is an operational observation, not a controlled quality comparison.                                                                                                                                                                                                                            |
| [`code.ask` cache threshold](#codeask-answer-cache-threshold)                         | Determine whether paraphrased questions can share an answer without sharing across materially different questions.                                                                                                         | 42 paraphrase pairs, 43 different-question pairs, 22 one-identifier near-misses and 2 unlabelled pairs.                                                                                                                                                            | About 130 embedding calls for each tested embedding model; cosine sweep with and without the identifier guard.                                                                                                                                                                                                 | Paraphrase hits versus false hits at each threshold; the shipped threshold requires zero false hits on the 65 labelled different pairs.                                                                                                                                                                                                                   |
| [Design decisions from design documents](#design-decisions-from-design-documents)     | Answer why questions, make change requests whose obvious move a design document rejected, answer questions on reversed or unrecorded decisions, and explain how a feature works, given different design knowledge per arm. | Private set: 5 design documents (4 revised once), 10 why, 15 change, 10 supersession or abstention and 13 feature tasks, 3 samples each.                                                                                                                           | Nothing, `knowledge.get`, knowledge hits of `loom_search`, gold rows or extracted rows through `memory.for_file` and `loom_search`, or extracted rows with the knowledge hits.                                                                                                                                 | `gpt-6-luna`-judged argument and feature-fact precision/recall, rejected-option avoidance, constraint satisfaction, stale statements and failed abstentions; paired against arm 0, the gold rows, and, for arm 6, the knowledge hits and the extracted rows alone.                                                                                        |
| [Document parsing](#document-parsing)                                                 | Read a PDF, DOCX or PPTX into text, headings, tables and page locators without losing content; then serve `knowledge.search` hits cited by page and quotes decision extraction can ground.                                 | Committed corpus of 54 files (6 public-domain PEPs, the rest synthetic), split dev and held-out by a fixed alternation within each category recorded in `gold.jsonl`; parse scores on the held-out 27; 212 search queries and 12 mined documents from both splits. | The reference extractor reading a PDF's text layer, and with its layout reader and OCR, and a layout-model baseline, through one normalizer; artifacts ingested through a stub extractor, fake n-gram embedder; `knowledge.search` top 5 within each document; decision extraction with `gpt-6-luna`, dry run. | Parse fidelity against gold labels from the specs the files were rendered from (text recall and precision, heading F1 and level, table cell F1, locators, silent loss) and efficiency (time and memory); retrieval (search found and cited at 1 and 5) and extraction quote match. Paired against the text layer with 95% bootstrap intervals over files. |

These protocols cover the public measurements, not every runnable harness. The
[coverage matrix](./coverage.md) marks surfaces that currently have only a harness or no
public measurement, so a product capability is never presented as a measured result.

### Models

Every benchmark server ran with the deployment's model settings, recorded field by field in
`bench/results/models.json` (`python -m bench.models_used`), except where a section says its
run does not record them.

| Job                                                             | Model                            |
| --------------------------------------------------------------- | -------------------------------- |
| Embeddings (memories, queries, code chunks)                     | `text-embedding-3-large`, 3072-d |
| Extraction, rerank (`llm_model`)                                | `gpt-6-luna`                     |
| `memory.ask` strong tier                                        | `claude-opus-5-5`                |
| `memory.ask` cheap tier                                         | `gpt-6-luna`                     |
| `memory.ask` mid tier                                           | off                              |
| Durability check on coding captures                             | `claude-opus-5-5`                |
| Session briefs, duplicate-check rerank and judge, bisect rerank | `gpt-6-luna`                     |
| Retrieval judge                                                 | `gpt-6.1-sol`                    |
| `code.search` HyDE passage                                      | `gpt-6-luna`                     |
| `code.ask` synthesis                                            | `gpt-6.1-sol`                    |

The reader and the judge of each benchmark are the bench's own and are named in its
section; they are pinned independently of the server's models. A reasoning model in the
bench (reader or judge) runs at the lowest reasoning effort it accepts unless its section
says otherwise. SWE-bench file localization and the design-rationale bench name their own
models.

### LoCoMo conversational QA

All 10 LoCoMo conversations (1,986 questions) ingested once through
`memory.set_from_messages`, then answered two ways over the same memory: by an external
reader from `memory.search` hits (top 10, the paper protocol), and by Loom's own
`memory.ask`. Categories 1–4 are answerable and scored by an LLM judge. Category 5 is
adversarial: a response is correct only when it abstains, and published LoCoMo comparisons
normally exclude this category.

| Category             |     n | External reader | `memory.ask` |
| -------------------- | ----: | --------------: | -----------: |
| Single-hop           |   841 |           88.8% |        93.0% |
| Multi-hop            |   282 |           66.0% |        87.9% |
| Temporal             |   321 |           78.8% |        87.5% |
| Open-domain          |    96 |           43.8% |        71.9% |
| **Categories 1–4**   | 1,540 |       **79.7%** |    **89.6%** |
| Adversarial (cat. 5) |   446 |           78.9% |        90.8% |

On answerable questions the external reader abstained 7.8% of the time. Search latency p50
1,478 ms, p95 3,604 ms; a full question (search and answer) p50 2,054 ms, p95 4,291 ms.
Each conversation left about 265 memories.

- **Models:** the server's above (extraction `gpt-6-luna`; `memory.ask` strong
  `claude-opus-5-5`, cheap `gpt-6-luna`). External reader `gpt-4.1-mini`; judge `gpt-4.1`
  (the harness's calibration on 100 known-correct and 100 known-wrong answers: TPR 1.00,
  FPR 0.01).
- **Artifacts:** `bench/results/locomo_qa.{csv,summary.json}` (external reader),
  `bench/results/locomo_qa_ask.{csv,summary.json}` (`memory.ask`).
- **Reproduce:** `python bench/locomo_qa.py --dataset bench/datasets/locomo10.json --base-url <server> --token <token> --org <org> --keep-namespaces --namespaces-json <map.json> --out <a>.csv`,
  then `--reader ask --namespaces-json <map.json> --out <b>.csv` answers the same memory
  with `memory.ask`; `--gate --compare A.csv B.csv` pairs two runs. Judged accuracy moves a
  few points between runs ([methodology](./methodology.md)).

### LongMemEval-S

All 500 LongMemEval-S questions, each over its own haystack of about 47 sessions ingested
through `memory.set_from_messages`, then answered over that memory two ways: by Loom's own
`memory.ask`, and by a pinned external reader given `memory.search`'s top 200 hits. Both
are graded by the official LongMemEval judge.

The `memory.ask` column measures Loom's end-to-end product configuration, including its
configured answer models. It is not the fixed-reader configuration normally submitted to
the LongMemEval leaderboard and should not be compared directly with systems using a
different reader. The separate `gpt-5` column holds the reader fixed.

| Question type             |   n | `memory.ask` | Reader `gpt-5`, top 200 | Recall@30 |
| ------------------------- | --: | -----------: | ----------------------: | --------: |
| Single-session user       |  70 |        97.1% |                   98.6% |     98.6% |
| Single-session assistant  |  56 |       100.0% |                   91.1% |     94.6% |
| Single-session preference |  30 |       100.0% |                  100.0% |     90.0% |
| Knowledge update          |  78 |       100.0% |                   92.3% |    100.0% |
| Temporal reasoning        | 133 |        97.0% |                   94.0% |    100.0% |
| Multi-session             | 133 |        91.7% |                   85.0% |    100.0% |
| **All**                   | 500 |    **96.6%** |               **92.0%** | **98.6%** |

Recall@30 is a per-question hit rate: a question counts when at least one of its labelled
evidence sessions is represented among the first 30 returned memories. It is not the mean
fraction of all supporting sessions retrieved.

The same answers graded by two more judges with the official prompt: `gpt-6.1-sol` 97.8%,
`claude-opus-5-5` 97.6%; the three judges agree on 98.8–99.8% of the questions. The
`memory.ask` answers written during ingest scored 97.2%. The external reader runs at
reasoning effort `medium`; at `minimal` the same hits scored 86.0% (in
`longmemeval_s_ingest.csv`, arm `reader_gpt5_k200`). `memory.ask` answers are not the
pinned-reader figure LongMemEval publications report; the reader arm is.

`memory.search` at `top_k` 15, one call at a time over the 500 questions: p50 881 ms,
p95 2,687 ms, 1,722 tokens of context (p50); the first questions run on cold caches.
Repeated 100-question passes on warm caches measure p50 449–487 ms and p95 2.3–3.4 s.
Extraction used about 1.5 M tokens per question; each question has its own history.

- **Models:** the server's above (`memory.ask` strong `claude-opus-5-5`, cheap
  `gpt-6-luna`); external reader `gpt-5`; judge `gpt-4o-2024-08-06` with the official
  prompt, re-graded by `gpt-6.1-sol` and `claude-opus-5-5`.
- **Artifacts:** `bench/results/longmemeval_s.csv` (every verdict above),
  `longmemeval_s_ingest.csv` (the ingesting run: recall, token use, its own answers),
  `longmemeval_s_judges.json` (the three judges and their agreement) and
  `longmemeval_s_latency.json`.
- **Reproduce:**
  `python bench/longmemeval.py --limit 500 --shuffle --seed 13 --answer-judge --keep-namespaces --ask-arms '[{"label":"ask"},{"label":"reader_gpt5_k200","answerer":"reader","reader_model":"gpt-5","top_k":200}]' --base-url <server> --token <token> --out <file>.csv --hyp-out <file>.hyp.jsonl`
  with `LOOM_LLM_OPENAI_REASONING_EFFORT=medium` for the reader; `--reuse-csv <file>.csv`
  answers an ingested run again;
  `python -m bench.rejudge --hyp <file>.hyp.jsonl --dataset bench/datasets/longmemeval_s_cleaned.json --judge gpt-4o-2024-08-06 --judge gpt-6.1-sol --judge claude-opus-5-5`;
  `python -m bench.lme_search_latency --csv <file>.csv --base-url <server> --token-file <file> --top-k 15`.

### MemBench

Both MemBench scenarios, 20 questions of each type, answered by `memory.ask` and scored as
multiple-choice accuracy.

| Scenario      | Questions |  Accuracy |
| ------------- | --------: | --------: |
| Observation   |       140 | **0.879** |
| Participation |       140 | **0.879** |

Per type: observation single-hop 0.95, multi-hop 1.00, comparative 0.80, aggregative 0.70,
knowledge update 0.85, post-processing 0.95, preference 0.90; participation single-hop
0.95, multi-hop 0.90, comparative 0.80, aggregative 0.60, knowledge update 1.00,
post-processing 0.95, preference 0.90. One question is 0.05 of a type. Evidence recall at
ingest was 1.000 (observation) and 0.986 (participation).

- **Models:** the server's above; no bench reader or judge (multiple choice).
- **Artifacts:** `bench/results/membench_{observation,participation}.json` (the answers
  above) and `membench_{observation,participation}_ingest.json` (the ingesting run, with
  the evidence recall).
- **Reproduce:**
  `python bench/membench.py --data-dir <Membenchdata> --scenario observation --limit 20 --seed 13 --base-url <server> --token <token> --org <org> --out <file>.json`
  (and `--scenario participation`); `--namespace-json <map>` re-asks an ingested run.

### MemoryAgentBench

Selected MemoryAgentBench contexts were memorized chunk by chunk through
`memory.set_from_messages` and answered by `memory.ask`. These are generated-answer
measurements, despite the source split being named “Accurate Retrieval”; they are not raw
`memory.search` recall measurements. SubEM and token F1 are scored on the answer with
Loom's inline citation markers (`[memory_key]`) removed; they are provenance, not answer
words.

| Split                                      | Contexts | Questions |     SubEM |        F1 |
| ------------------------------------------ | -------: | --------: | --------: | --------: |
| Accurate retrieval (`eventqa_65536`)       |        5 |       500 | **0.990** | **0.957** |
| Conflict resolution (`factconsolidation_`) |        8 |       800 | **0.772** | **0.227** |
| Accurate retrieval (`ruler_qa1_197K`)      |        1 |       100 | **0.960** |     0.097 |
| Test-time learning (`icl_banking77`)       |        1 |       100 | **0.950** |         — |

Conflict resolution by context: single-hop 6k 0.93, 32k 0.94, 64k 0.92, 262k 0.89;
multi-hop 6k 0.77, 32k 0.71, 64k 0.56, 262k 0.46. Accurate retrieval on `eventqa_65536`
answered during ingest scored SubEM 0.994 and F1 0.928.

The RULER 197K context is one 197K-token document (497 chunks). Its answers state the
answer, then the supporting memories, so F1 against a short gold is low; counting only an
answer's first sentence, RULER scores 0.91. The test-time-learning score is the harness's
rule (the gold label appears in the answer); an answer often weighs several labels, and
counting only the first label it names scores 0.85.

- **Models:** the server's above; no bench reader or judge (SubEM, F1).
- **Artifacts:** `bench/results/memoryagentbench_ar.json` (the accurate-retrieval answers
  above), `memoryagentbench_ar_ingest.jsonl` (the ingesting run),
  `memoryagentbench_cr.jsonl`, `memoryagentbench_ruler197k.jsonl` and
  `memoryagentbench_ttl_banking77.jsonl`.
- **Reproduce:**
  `python bench/memoryagentbench.py --data <export.jsonl> --splits Accurate_Retrieval --source-regex eventqa_65536 --max-contexts 5 --max-questions 100 --memorize extract --answer ask --base-url <server> --token <token> --org <org> --out <file>.jsonl`
  (and `--splits Conflict_Resolution --source-regex factconsolidation_ --max-contexts 8`;
  `--splits Accurate_Retrieval --source-regex ruler_qa1_197K --max-contexts 1 --max-questions 0`;
  `--splits Test_Time_Learning --source-regex icl_banking77 --max-contexts 1 --max-questions 0`).

### MuSiQue-Ans

200 multi-hop questions sampled from the MuSiQue-Ans dev set (seed 0): each question's
paragraphs written through `memory.set`, retrieved by `memory.search` (semantic, top 10)
and answered by a pinned external reader. Recall@k is the mean fraction of each question's
gold supporting paragraphs present in the first `k` memories; it is not the share of
questions with any evidence hit.

| Measure   |     Value |
| --------- | --------: |
| EM        | **45.5%** |
| F1        | **55.3%** |
| Recall@10 | **0.890** |

By hops: recall@5 0.80 (2-hop), 0.77 (3-hop), 0.58 (4-hop). Search p50 982 ms.

- **Models:** embeddings `text-embedding-3-large` (no server LLM on this path); reader
  `gpt-4o-mini`; EM and F1 against the gold answer, no LLM judge.
- **Artifacts:** `bench/results/musique.{summary.json,jsonl}`.
- **Reproduce:**
  `python bench/musique.py --data musique_ans_v1.0_dev.jsonl --n 200 --seed 0 --top-k 10 --search-json '{"search_mode":"semantic"}' --reader --base-url <server> --token <token> --org <org> --out <file>.jsonl`.

### PerLTQA

The published sample of PerLTQA (5 characters, up to 10 questions per section: 193
questions): profile, social relationships, events and dialogues ingested through
`memory.set_from_messages`, answered by `memory.ask` and graded against the reference
answer.

| Section             |   n |  Accuracy |
| ------------------- | --: | --------: |
| Profile             |  43 |     0.954 |
| Social relationship |  50 |     0.880 |
| Events              |  50 |     0.920 |
| Dialogues           |  50 |     0.540 |
| **All**             | 193 | **0.819** |

The same answers graded by two more judges with the harness's prompt: `gpt-6.1-sol` 0.756,
`claude-opus-5-5` 0.839. Repeated runs (fresh ingests and re-asks) span 0.808–0.829, and
re-asking one store moves about 10 answers. Dialogues score lowest: their reference answers
often paraphrase the transcript ("the AI assistant mentioned private celebrations" for a
dinner a character arranged), and the grader rejects an answer that states the
transcript's own words.

- **Models:** the server's above; judge `gpt-4o-mini` (harness), re-graded by `gpt-6.1-sol`
  and `claude-opus-5-5` with the harness's prompt.
- **Artifacts:** `bench/results/perltqa.{summary.json,jsonl}`,
  `perltqa_judges.json`.
- **Reproduce:**
  `python bench/perltqa.py --mem perltmem_en_v2.json --qa perltqa_en_v2.json --max-characters 5 --max-questions-per-section 10 --namespace-json '{"default_conflict_policy":"newest_wins"}' --judge-model gpt-4o-mini --base-url <server> --token <token> --org <org> --out <file>.jsonl`.

### Duplicate check on public ClickHouse issues

`bench/tracker_dupcheck_bench.py` replays public ClickHouse issues closed as duplicates of an
older issue through `tracker.duplicate_check`, each as a new draft: its title and body,
`as_of` its creation time, and no issue number, so the op sees neither the item nor the
recorded link. 100 issues never closed as duplicates and carrying no duplicate or related
link are replayed the same way as negative-labelled examples. That label means the public
tracker has no recorded duplicate evidence; it does not prove the issues are unrelated.

The result is one tracker's. `tracker.duplicate_check` reads the tracker records bound to
the namespace (and the other tracker namespaces the caller may read), not the code index:
adding a repository indexes its code and merged PR history, never its issues, and a
repository with no tracker source gets no duplicate check. ClickHouse's public issues are
easier than many trackers' (stack traces, error codes, versions and SQL in the body, and
duplicates closed and linked consistently), so a team's own tracker can score differently;
the harness measures it on any tracker that serves the record contract's `github_issues`
and `edges` and records a duplicate as `state_reason = 'duplicate'` with a `related_to`
edge to the older item.

| Measure                                                |            Value |
| ------------------------------------------------------ | ---------------: |
| Older issue ranked first (104 duplicates)              |            0.875 |
| `duplicate_of` verdicts naming the right issue first   | 0.989 (90 of 91) |
| Verdicts on duplicates (duplicate_of / related / none) |      91 / 2 / 11 |
| No-recorded-link issues called a duplicate (100)       |             0.02 |
| Wall time p50 / p95                                    | 1,912 / 3,472 ms |

- **Models:** embeddings `text-embedding-3-large`; rerank `gpt-6-luna`; judge `gpt-6-luna`
  (`llm_model`); no bench judge (hit against the recorded duplicate link).
- **Artifacts:** `bench/results/tracker_dupcheck.json` (summary and every replayed
  draft's verdict and ranked numbers).
- **Reproduce:**
  `python -m bench.tracker_dupcheck_bench --base-url <server> --token-file <viewer token for the tracker namespace> --env-file <env with the tracker credential> --namespace <tracker namespace> --repo <owner/repo> --limit 200 --negatives 100 --out <file>.json`;
  `--namespace` and `--repo` point it at another tracker.

### Code intelligence on the ClickHouse repository

`bench/code_intel_qa.py` over its 60 authored questions (`bench/fixtures/code_intel_qa.jsonl`)
against an index of the public ClickHouse repository.

| Family                   |   n | Result                                              |
| ------------------------ | --: | --------------------------------------------------- |
| Symbol definitions       |  10 | exact@1 1.00                                        |
| Callers                  |   4 | F1 0.93                                             |
| Conceptual code search   |  36 | recall@5 0.91, recall@10 0.97, MRR 0.91, hit@5 1.00 |
| Out-of-repository probes |  10 | abstain precision 1.00, recall 0.60                 |

Answers returned a mean of 2,345 tokens (p90 2,930); `code.search` p50 was 2,799 ms. All 46
answers came from one index revision, which was behind the repository's newest commit, so
each carried the stale flag. Repeated runs move conceptual recall@5 by about 0.02.

For conceptual search, recall@k is the mean fraction of every question's labelled files
found in the first `k`; hit@5 asks only whether at least one labelled file appeared. Thus
the same run has recall@5 0.912 and hit@5 1.00—every question has a hit, but some questions
have additional labelled files outside the first five.

- **Models:** the server's above: embeddings `text-embedding-3-large`, `code.search`
  hypothetical-answer passage `gpt-6-luna`; no LLM judge.
- **Artifacts:** `bench/results/code_intel_qa.summary.txt` (the harness's
  summary) and `bench/results/code_intel_qa.jsonl` (every question and answer).
- **Reproduce:** `python bench/code_intel_qa.py --base-url <server> --token <viewer token> --org <org> --namespace <code namespace> --verbose --dump bench/results/code_intel_qa.jsonl`.
  The sets are small: one caller question moves the F1 by about 0.25.

### SWE-bench Verified file localization

This is a **file-localization benchmark**, not the official SWE-bench Verified
issue-resolution evaluation. Loom does not generate a patch, run the repository's tests
or receive credit for resolving an issue. The official benchmark evaluates generated
patches on a [human-validated set of 500 instances](https://www.swebench.com/verified.html).

`bench/swebench_localization.py` asks `code.search` with each instance's issue text
(`problem_statement`) and scores the files its gold `patch` modifies. No LLM judge. A
stratified sample of 200 eligible instances from the 500-instance dataset (seed 7, every
repository represented in proportion: django 91, sympy 29, sphinx 18, matplotlib 14,
scikit-learn 13, astropy 9, xarray 9, pytest 8, pylint 4, requests 3, seaborn 1, flask 1).
An instance is eligible when its accepted patch modifies at least one file that existed at
`base_commit`; instances whose patch only adds files are excluded because there is no
pre-existing file to retrieve.

| Arm                                      | hit@1 | hit@5 | hit@10 | all gold@10 |  MRR |
| ---------------------------------------- | ----: | ----: | -----: | ----------: | ---: |
| `code.search` (first 25 units, as files) |  0.44 | 0.765 |   0.82 |       0.755 | 0.58 |
| `code.search` first page only (10 units) |  0.44 |  0.77 |   0.82 |        0.75 | 0.57 |
| BM25, every indexed file                 |  0.19 |  0.45 |   0.57 |        0.50 | 0.31 |
| BM25, `.py` files only                   |  0.42 |  0.68 |   0.79 |        0.71 | 0.54 |

Paired over the 200 instances, `code.search` minus BM25 over the same files is +0.25
hit@10 (95% bootstrap interval [+0.18, +0.32]) and +0.27 MRR; against BM25 restricted to
`.py` files it is +0.035 hit@10 ([−0.025, +0.095]) and +0.03 MRR, not distinguishable.
37% of the top-5 units are prose (release notes, guides, changelogs) although every gold
file is source; a prose file ranks first on 49 of 200 issues. 57 issue texts are longer than 2,000 characters and were
sent cut to 2,000: hit@10 0.68 on those against 0.87
on the rest. `code.search` p50 1,939 ms, p95 4,462 ms; a first page returned a mean of
2,432 tokens. No answer abstained.

The table ran without the prose penalty, which ranks prose files below source unless the
question asks for documentation (`code_search_prose_penalty`, 0.25). Re-asked on the 12 instances whose repositories are
still indexed at the instance's commit, the gold file ranks first on 6 instead of 4, and
prose takes 8 instead of 27 of the 60 top-5 slots; hit@5 and hit@10 are unchanged (9 of 12).
Replaying the 200 stored rankings with prose scaled by 0.25 gives hit@1 0.53 and hit@5 0.81
(from 0.44 and 0.77), a lower bound since a replay cannot pull in files past the 25 units
it holds. The 36 conceptual questions above are unchanged by it (recall@10 0.963 without,
0.972 with).

- **Protocol:** gold = pre-image paths of files the patch modifies or deletes that exist
  at `base_commit`; patch-added files are excluded and `test_patch` is never read. One
  namespace per repository, backfilled with `--embed` at its oldest
  sampled `base_commit`, then moved commit to commit in commit-time order (deleted paths
  tombstoned, changed paths re-ingested with `loom code backfill --paths-file --commit`),
  so each query sees exactly its instance's tree. A step larger than 3,000 changed files
  starts a fresh backfill instead. Query: the issue text, cut to 2,000 characters;
  `top_k=10`, server-default bodies and legs, `fresh=true`; a second page through
  `next_cursor` deepens the ranking to 25 units. BM25 (k1 1.2, b 0.75, path plus content,
  identifier splitting) runs on the same query over the files the namespace holds live
  at that commit, and over the tree's `.py` files.
- **Models and configuration:** embeddings `text-embedding-3-large`, 512-dimension prefix
  shortlist, card and concept legs, hypothetical-answer passage written by `gpt-4o` (not the
  model in the table above); no chunk descriptions or file cards (a fresh
  backfill writes neither); one server against ClickHouse Cloud.
- **Cost:** 137.5 M embedding tokens (about $18 at list price) for 11 backfills and 188
  deltas; about 10.6 h of indexing over two repositories at a time.
- **Artifacts:** `bench/results/swebench_localization.summary.json` (every
  figure above, per repository, and the indexing cost) and
  `bench/results/swebench_localization.jsonl` (every instance: gold, ranked
  files of each arm, route, confidence, tokens, latency, index step).
- **Reproduce:** with a bench token and `OPENAI_API_KEY`,
  `python -m bench.swebench_localization run --sample 200 --seed 7 --base-url <server> --token-file <file> --env-file <the server's .env> --repos-dir <clones> --delta-mode repair --rebase-files 3000 --chain-files 1000000000 --out <rows.jsonl>`,
  then `python -m bench.swebench_localization bm25-py --rows <rows.jsonl> --repos-dir <clones>`
  and `python -m bench.swebench_localization report --rows <rows.jsonl>`. The run is
  resumable. At n=200 one instance moves a rate by 0.5 points.

### With and without Loom

What an agent or reader spends and gets with Loom against the same task without it. The
coding comparisons count the agent's own model bill; Loom's server-side model calls are not
included.

**Memory instead of the whole history (LongMemEval-S).** 100 questions (seed 13) answered
by `gpt-5` (reasoning effort `medium`) twice: from the whole chat history in the prompt,
and from `memory.search`'s top 200 hits over the ingested memory. Both are graded by the
official judge.

| Reader input            | Accuracy | Reader prompt tokens per question |
| ----------------------- | -------: | --------------------------------: |
| Whole history           |      90% |                           103,879 |
| Loom's top 200 memories |  **94%** |                         **9,443** |

Paired, 87 questions are right both ways, 7 only with memory, 3 only with the whole history.
The four-point accuracy difference is descriptive: only 10 outcomes disagree, and a
two-sided exact McNemar test gives p=0.34. This sample does not establish that retrieval is
more accurate than full history. Memory reads 91% fewer reader prompt tokens per question.
Those prompt-token totals exclude memory construction. Building the memory costs about
1.5 M extraction tokens per haystack, once, on `gpt-6-luna`. Against about 94 k
tokens saved per question, that is repaid after about 16 questions over the same history,
before the price gap between the extraction model and the reader is counted.

**Coding agents on ClickHouse, with and without Loom.** Claude Code (`claude-sonnet-5`) answers
each question twice in a stock session (no user or project settings): with Bash, Read, Grep and
Glob, and with the same tools plus the Loom plugin (hooks, and the MCP `coding` toolset with
`code.search` and `code.symbol` loaded up front). Sub-agents are off in both arms. Cost is the
Claude Code session's model bill (Loom's server-side model calls are not in it); every tool
call is counted per run, and a run that ends out of turns without an answer
counts as a miss at its full cost.

Each set runs on a checkout without git history at the commit the questions were
verified at (history questions keep full history), so neither arm can read a fix out of `git log`:

- **Answer-graded real work** (`bench/fixtures/code_work_v2.jsonl`, 24 questions, 3 paired runs):
  explain a mechanism end to end, list every caller and override a change must update, and find
  which merged change made a behaviour change. The agent answers in prose; `bench.agent_work_judge`
  (`claude-opus-5-5`) marks each key point of a reference answer verified in the code covered or
  not, and halves the score of a reply that contradicts it.
- **Conceptual questions** (`bench/fixtures/code_intel_qa.jsonl`, 36 questions, 10 paired runs):
  where a mechanism lives, scored by whether the right file is among the three files the agent
  ends its reply with.
- **Multi-hop work** (`bench/fixtures/code_work_multihop.jsonl`, 24 questions, 3 paired runs): a
  bug report traced to its fix on a release branch whose fix was never backported (the branch
  checkout and the branch's own index), the tests that would catch a change, and setting- or
  error-to-call-site chains; answer-graded like the real-work set.

| Set                           | Quality, without → with Loom | Model cost per question, without → with | Turns, without → with |
| ----------------------------- | ---------------------------- | --------------------------------------- | --------------------- |
| Answer-graded real work (all) | judged score 0.78 → **0.84** | $0.212 → **$0.170** (−20%)              | —                     |
| — explain                     | 0.72 → **0.80**              | $0.312 → **$0.251** (−19%)              |                       |
| — impact                      | 0.88 → **0.92**              | $0.190 → **$0.148** (−22%)              |                       |
| — history                     | 0.73 → **0.81**              | $0.135 → **$0.110** (−18%)              |                       |
| Conceptual questions          | right file 91.4% → **96.4%** | $0.096 → **$0.076** (−21%)              | 5.5 → 5.7             |
| Multi-hop work (all)          | judged score 0.72 → **0.76** | $0.423 → **$0.396** (−6%)               | —                     |
| — tests that cover a change   | 0.76 → **0.81**              | $0.193 → **$0.154** (−21%)              |                       |
| — call chains                 | 0.87 → **0.89**              | $0.266 → **$0.223** (−16%)              |                       |
| — bug to fix, release branch  | 0.53 → **0.57**              | $0.811 → $0.811                         |                       |

Per question, the cost is compared against the same arm's run-to-run noise: two batches of the
without arm differ by more than 10% on a third of the questions at three runs each, so a question
counts as dearer with Loom only when the 90% bootstrap bound of its cost ratio stays above 1.
With Loom a question takes longer on the clock: the plugin's per-prompt search and the MCP
round trips add latency.

An earlier 12-issue SWE-bench agent sample is withdrawn: its with-Loom agent called no Loom tool.

**Definitions and callers against grep.** The 10 definition and 4 caller questions of
`bench/fixtures/code_intel_qa.jsonl`, replayed as `git grep` over a ClickHouse checkout:
once for the bare name, as an agent searching for a word would, and once for `Class::name`,
as a careful one would. Tokens are characters / 4.

| Question    | Bare-name grep, median           | `Class::name` grep, median    | Loom                                     |
| ----------- | -------------------------------- | ----------------------------- | ---------------------------------------- |
| Definitions | 29,061 tokens over 227 files     | 1,623 tokens over 25 files    | 1,678 tokens; right file first, 10 of 10 |
| Callers     | 558 tokens; 4 of 12 callers seen | 101 tokens; misses call sites | 12 of 12 expected callers                |

For a method with a distinctive qualified name, a careful grep is as cheap as Loom. For a
free function or a common type name (`executeQuery`, `parseQuery`, `Field`) grep returns
35 k to 490 k tokens where Loom returns about 1.2 k to 2.5 k. For callers grep is cheap but
incomplete: a call site spells the receiver, not the class (`reader.readFromParts(…)`), so
the qualified grep misses them and the bare grep names a third of them.

**Repeated `code.ask` questions.** Over 30 days of one deployment's real (non-bench)
traffic, 20 of 673 `code.ask` calls (3%) were served from the answer cache: each saved a
`gpt-6.1-sol` answer of about 3,700 tokens and took 385 ms instead of 7.8 s, about 74 k
tokens in all. The cache works but repeats are rare; how often it serves a wrong answer is
the threshold sweep below (0 of 65 different pairs at the shipped threshold).

- **Artifacts:** `bench/results/longmemeval_s_full_history.json` (every paired question),
  `agent_clickhouse_work.jsonl`, `agent_clickhouse_multihop.jsonl` and `agent_conceptual_ab.jsonl`
  (every run's arm, tokens, turns, Loom calls, cost, files, scores and judged key points),
  `code_grep_baseline.json` and `code_ask_cache_production.json`.
- **Reproduce:**
  `python -m bench.lme_full_context --dataset bench/datasets/longmemeval_s_cleaned.json --pair bench/results/longmemeval_s.csv --limit 100`
  (with `LOOM_LLM_OPENAI_REASONING_EFFORT=medium`);
  `python -m bench.agent_localization_ab --questions <fixture> --checkout <checkout at the pinned commit> --namespace <ns> --org <org> --mcp-url <server>/mcp?toolset=coding --token-file <file> --plugin-dir integrations/claude-code-plugin --code-repo ClickHouse/ClickHouse --unique-prompts --out <file>.jsonl`
  then `python -m bench.agent_work_judge --runs <file>.jsonl --fixture bench/fixtures/code_work_v2.jsonl --out <judged>.jsonl`;
  (`--session bare` for the up-front listing);
  `python -m bench.code_grep_baseline --checkout <ClickHouse checkout> --loom bench/results/code_intel_qa.jsonl`.

### `code.ask` answer-cache threshold

`code.ask` serves a cached answer when the nearest cached question clears a per-model
cosine threshold and passes the identifier guard (`guard_passes` in
`server/code/app/answers_cache.py`: both questions must name the same identifiers,
settings, paths, numbers, quoted strings and error codes). The sweep measures the trade
between cache hits and serving an answer to a different question.

| Artifact                                             | Content                                                                                                                                                                             |
| ---------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `bench/code_ask_cache_threshold/pairs.json`          | 42 `paraphrase` pairs (should share an answer), 43 `different` pairs (mostly the same subsystem), 22 `different_identifier` near-misses that differ in one identifier, 2 unlabelled |
| `bench/code_ask_cache_threshold/summary.json`        | The sweep under `text-embedding-3-large` (3072 dimensions)                                                                                                                          |
| `bench/code_ask_cache_threshold/summary_3small.json` | The sweep under `text-embedding-3-small` (1536 dimensions)                                                                                                                          |
| `bench/code_ask_cache_threshold/measure.py`          | The script that produced both                                                                                                                                                       |

Each summary records the embedding `model` and `dim`, not a commit or the guard's
version; `git log` on the file is the provenance.

**Reproduce** (about 130 embedding calls; needs `OPENAI_API_KEY`):

```bash
.venv/bin/python bench/code_ask_cache_threshold/measure.py --env-file .env \
  --model text-embedding-3-large --out /tmp/summary.json
.venv/bin/python bench/code_ask_cache_threshold/measure.py --env-file .env \
  --model text-embedding-3-small --out /tmp/summary_3small.json
diff <(jq -S . /tmp/summary.json) <(jq -S . bench/code_ask_cache_threshold/summary.json)
```

Embedding APIs are not bit-stable across provider revisions: expect cosines to agree to a
few thousandths. A threshold is robust only when its margin exceeds that drift.

**With the guard on** (`sweep_with_guard`: paraphrase hits of 42 and false hits of 65
different pairs):

| Threshold | 3-large paraphrase hits | 3-large false hits | 3-small paraphrase hits | 3-small false hits |
| --------- | ----------------------- | ------------------ | ----------------------- | ------------------ |
| 0.70      | 36                      | 7                  | 36                      | 6                  |
| 0.75      | 30                      | 5                  | 29                      | 4                  |
| 0.80      | 29                      | 2                  | 24                      | 1                  |
| 0.85      | 14                      | 0                  | 17                      | 1                  |
| 0.90      | 6                       | 0                  | 5                       | 0                  |

**Recorded choices:**

| Field                  | Rule                                                                           | 3-large                                   | 3-small                       |
| ---------------------- | ------------------------------------------------------------------------------ | ----------------------------------------- | ----------------------------- |
| `chosen`               | Lowest 0.01 step above every `different` pair, cosine alone                    | 0.88: 12 of 42 paraphrases, margin 0.0003 | 0.87: 12 of 42, margin 0.0063 |
| `chosen_all_different` | Cosine alone, including the one-identifier near-misses                         | 0.99: 0 of 42                             | 0.99: 0 of 42                 |
| `chosen_with_guard`    | Lowest step with no different pair that clears the cosine and passes the guard | 0.84: 17 of 42, margin 0.0056             | 0.87: 11 of 42, margin 0.0063 |

Cosine alone cannot separate a question from its one-identifier twin (two issue numbers
one apart sit at 0.9857 under 3-large), so without the guard the only safe threshold
serves no paraphrase. The guard refuses all 22 near-misses and 12 of the 43 different
pairs, which lets the threshold drop to where a third of paraphrases hit.

**What ships.** `CACHE_HIT_THRESHOLDS` serves 0.85 for `text-embedding-3-large` (one step
above 0.84: 14 hits instead of 17, margin 0.0156 instead of 0.0056) and 0.87 for
`text-embedding-3-small`. An unlisted model takes 0.87; `LOOM_CODE_ASK_CACHE_THRESHOLD`
(> 0) overrides all. The guard cannot be turned off, so the unguarded columns are context.

**Limits.** One pair moves a hit rate by more than two points. The pairs come from one
deployment's cached questions over two repositories. Re-run the script for a new embedder
before serving the cache on it.

### Design decisions from design documents

`bench/design_rationale/` asks a coding agent four kinds of task about code its team's
design documents describe: why questions (A), change requests whose obvious move is an
option the documents rejected or a constraint they set (B), questions about a statement
a later revision reversed or about code no document covers (C), and questions about how
a feature works, its limits, compatibility and dependencies (D). Each arm shows the
agent different design knowledge: nothing (0), the whole document through
`knowledge.get` (1), the document as knowledge through the knowledge hits of
`loom_search` (2), hand-written gold decision rows through the pre-edit block and the
memory section of `loom_search` (3), the rows `loom decisions extract --apply` mined
from the same documents, through the same surfaces (4), and those extracted rows with
the documents' knowledge hits of `loom_search` (6). Arm 4 approximates the sync leg:
documents ingested as a document source's sync writes them, mined by the same extraction
function. The extractor in this run read 6,000-character windows and kept a decision
only when its reasoning and each rejected option's reason were one verbatim span of one
chunk; the whole-document extractor with cited, checked claims is not in these tables.
Extracted rows get files from a read-only code index of the ClickHouse repository, so
arms 4 and 6 read the pre-edit block too.

The task set is private: five design documents for the ClickHouse repository, four of them
revised once for the supersession tasks; 10 A, 15 B, 10 C (5 supersession, 5 abstention)
and 13 D tasks. The C revisions and abstention questions are synthetic. Each task is
answered 3 times per arm; each answer is judged once in each order.

| Arm | A precision | A recall | A misleading | A abstains | B avoids | B satisfies | C passes | C states current | D precision | D recall | D misleading | D abstains | Citation precision | Citation recall |
| --- | ----------: | -------: | -----------: | ---------: | -------: | ----------: | -------: | ---------------: | ----------: | -------: | -----------: | ---------: | -----------------: | --------------: |
| 0   |        29.3 |     30.1 |         78.3 |      100.0 |     18.9 |        12.2 |     93.3 |             13.3 |        15.6 |     12.1 |         65.4 |      100.0 |                  - |             0.0 |
| 1   |        84.1 |     92.1 |         25.0 |       20.0 |      4.4 |         2.2 |    100.0 |            100.0 |        89.5 |     76.0 |         23.1 |        0.0 |               83.4 |            79.0 |
| 2   |        81.3 |     80.6 |         41.7 |       26.7 |     42.2 |        43.3 |    100.0 |            100.0 |        80.3 |     59.7 |         47.4 |       10.3 |               54.5 |            48.9 |
| 3   |        96.8 |     88.8 |          8.3 |       10.0 |     87.8 |        81.1 |    100.0 |            100.0 |        91.9 |     65.5 |         16.7 |        6.4 |               94.0 |            94.2 |
| 4   |        53.6 |     32.9 |         36.7 |       63.3 |     54.4 |        52.2 |     96.7 |             10.0 |        65.0 |     26.9 |         37.2 |       69.2 |               92.1 |            63.8 |
| 6   |        84.8 |     83.9 |         26.7 |       20.0 |     55.6 |        55.6 |    100.0 |            100.0 |        85.4 |     67.7 |         42.3 |        5.1 |               84.0 |            74.9 |

Percentages. A and D precision is the share of an answer's claims that state a gold
argument (A) or gold fact (D) or what a gold row states, over the answers that made a
claim; recall is the share of the gold arguments or facts the answer states; A and D
abstain are the share of answers saying no recorded decision (A) or design (D) covers the
question, in whole or in part; A and D misleading are the share of answers that would
steer an engineer wrong. B avoids: no rejected option proposed (deterministic detectors
first, the judge for the rest) and the rubric passed where the task has one; B satisfies:
no gold constraint broken, no rejected option detected, and the rubric passed where the
task has one. C passes: a supersession answer does not present a reversed statement as the
current design, an abstention answer says no decision is recorded and invents none. C
states current is averaged over the 5 supersession tasks. A reversed statement was
presented as current in 6.7% of the supersession answers in arms 0 and 4, none in the
other arms; 6.7% of the abstention answers failed in arm 0, none in the other arms.
Citation precision is the share of the answers' citations that resolve and that the judge
says support what they are cited for. Citation recall is the share of A and D claims with
at least one of their own citations resolving and supporting them. Answers with no claim,
which precision leaves out: of the A answers 6.7% in arm 4, of the D answers 10.3% in arm
0 and 17.9% in arm 4, none in the other arms.

Paired per task, bootstrap over documents, 95% interval, and the minimum detectable effect
taken from that interval. With five clusters the bootstrap intervals and the MDEs taken
from them understate the uncertainty:

| Treatment vs reference | Kind | Metric    | n (clusters) | Delta | 95% interval   |  MDE |
| ---------------------- | ---- | --------- | -----------: | ----: | -------------- | ---: |
| 3 vs 0                 | A    | precision |       10 (5) | +67.5 | +61.0 .. +82.7 | 15.5 |
| 3 vs 0                 | A    | recall    |       10 (5) | +58.7 | +53.1 .. +73.8 | 14.8 |
| 3 vs 0                 | B    | avoids    |       15 (5) | +68.9 | +59.5 .. +86.7 | 19.4 |
| 3 vs 0                 | B    | satisfies |       15 (5) | +68.9 | +40.9 .. +89.4 | 34.6 |
| 3 vs 0                 | C    | passed    |       10 (5) |  +6.7 | +0.0 .. +12.1  |  8.7 |
| 3 vs 0                 | D    | precision |       13 (5) | +76.3 | +60.5 .. +87.2 | 19.0 |
| 3 vs 0                 | D    | recall    |       13 (5) | +53.3 | +41.0 .. +71.6 | 21.8 |
| 4 vs 0                 | A    | precision |       10 (5) | +24.3 | -9.5 .. +45.8  | 39.5 |
| 4 vs 0                 | A    | recall    |       10 (5) |  +2.8 | -18.1 .. +16.2 | 24.4 |
| 4 vs 0                 | B    | avoids    |       15 (5) | +35.6 | +0.0 .. +59.5  | 42.5 |
| 4 vs 0                 | B    | satisfies |       15 (5) | +40.0 | +8.3 .. +61.1  | 37.7 |
| 4 vs 0                 | C    | passed    |       10 (5) |  +3.3 | +0.0 .. +8.3   |  5.9 |
| 4 vs 0                 | D    | precision |       11 (5) | +46.5 | +1.3 .. +69.3  | 48.6 |
| 4 vs 0                 | D    | recall    |       13 (5) | +14.7 | -0.7 .. +22.4  | 16.5 |
| 4 vs 3                 | A    | precision |       10 (5) | -43.2 | -81.0 .. -22.9 | 41.5 |
| 4 vs 3                 | A    | recall    |       10 (5) | -55.9 | -83.3 .. -39.4 | 31.4 |
| 4 vs 3                 | B    | avoids    |       15 (5) | -33.3 | -64.1 .. -11.9 | 37.3 |
| 4 vs 3                 | B    | satisfies |       15 (5) | -28.9 | -61.5 .. -8.3  | 38.0 |
| 4 vs 3                 | C    | passed    |       10 (5) |  -3.3 | -10.0 .. +0.0  |  7.1 |
| 4 vs 3                 | D    | precision |       11 (5) | -29.2 | -59.5 .. -15.1 | 31.7 |
| 4 vs 3                 | D    | recall    |       13 (5) | -38.6 | -56.3 .. -28.2 | 20.1 |
| 1 vs 0                 | A    | precision |       10 (5) | +54.8 | +47.9 .. +69.5 | 15.4 |
| 1 vs 0                 | A    | recall    |       10 (5) | +62.0 | +48.7 .. +88.1 | 28.1 |
| 1 vs 0                 | B    | avoids    |       15 (5) | -14.4 | -22.0 .. -3.0  | 13.5 |
| 1 vs 0                 | B    | satisfies |       15 (5) | -10.0 | -15.2 .. -1.5  |  9.7 |
| 1 vs 0                 | C    | passed    |       10 (5) |  +6.7 | +0.0 .. +12.1  |  8.7 |
| 1 vs 0                 | D    | precision |       13 (5) | +73.9 | +59.0 .. +82.1 | 16.4 |
| 1 vs 0                 | D    | recall    |       13 (5) | +63.8 | +54.7 .. +75.3 | 14.7 |
| 1 vs 3                 | A    | precision |       10 (5) | -12.7 | -20.2 .. -6.9  |  9.5 |
| 1 vs 3                 | A    | recall    |       10 (5) |  +3.3 | -7.1 .. +22.5  | 21.2 |
| 1 vs 3                 | B    | avoids    |       15 (5) | -83.3 | -94.0 .. -70.0 | 17.2 |
| 1 vs 3                 | B    | satisfies |       15 (5) | -78.9 | -97.0 .. -42.4 | 39.0 |
| 1 vs 3                 | C    | passed    |       10 (5) |  +0.0 | +0.0 .. +0.0   |  0.0 |
| 1 vs 3                 | D    | precision |       13 (5) |  -2.4 | -10.6 .. +3.6  | 10.2 |
| 1 vs 3                 | D    | recall    |       13 (5) | +10.5 | +4.4 .. +15.5  |  7.9 |
| 2 vs 3                 | A    | precision |       10 (5) | -15.6 | -27.5 .. -7.6  | 14.2 |
| 2 vs 3                 | A    | recall    |       10 (5) |  -8.2 | -17.6 .. +8.5  | 18.7 |
| 2 vs 3                 | B    | avoids    |       15 (5) | -45.6 | -52.8 .. -40.2 |  9.0 |
| 2 vs 3                 | B    | satisfies |       15 (5) | -37.8 | -52.8 .. -19.1 | 24.1 |
| 2 vs 3                 | C    | passed    |       10 (5) |  +0.0 | +0.0 .. +0.0   |  0.0 |
| 2 vs 3                 | D    | precision |       13 (5) | -11.6 | -14.1 .. -7.5  |  4.7 |
| 2 vs 3                 | D    | recall    |       13 (5) |  -5.8 | -13.8 .. +1.9  | 11.2 |
| 6 vs 2                 | A    | precision |       10 (5) |  +3.5 | -0.4 .. +10.3  |  7.6 |
| 6 vs 2                 | A    | recall    |       10 (5) |  +3.3 | -7.4 .. +8.6   | 11.4 |
| 6 vs 2                 | B    | avoids    |       15 (5) | +13.3 | -0.9 .. +38.3  | 28.0 |
| 6 vs 2                 | B    | satisfies |       15 (5) | +12.2 | -3.0 .. +38.3  | 29.5 |
| 6 vs 2                 | C    | passed    |       10 (5) |  +0.0 | +0.0 .. +0.0   |  0.0 |
| 6 vs 2                 | D    | precision |       13 (5) |  +5.1 | -4.1 .. +9.9   | 10.0 |
| 6 vs 2                 | D    | recall    |       13 (5) |  +8.0 | +1.3 .. +15.4  | 10.1 |
| 6 vs 4                 | A    | precision |       10 (5) | +31.1 | +13.5 .. +68.3 | 39.1 |
| 6 vs 4                 | A    | recall    |       10 (5) | +51.0 | +31.9 .. +80.2 | 34.4 |
| 6 vs 4                 | B    | avoids    |       15 (5) |  +1.1 | -27.3 .. +38.9 | 47.3 |
| 6 vs 4                 | B    | satisfies |       15 (5) |  +3.3 | -25.0 .. +40.3 | 46.6 |
| 6 vs 4                 | C    | passed    |       10 (5) |  +3.3 | +0.0 .. +10.0  |  7.1 |
| 6 vs 4                 | D    | precision |       11 (5) | +22.4 | +9.6 .. +44.8  | 25.2 |
| 6 vs 4                 | D    | recall    |       13 (5) | +40.8 | +29.1 .. +57.1 | 20.0 |
| 6 vs 3                 | A    | precision |       10 (5) | -12.1 | -20.1 .. -5.4  | 10.5 |
| 6 vs 3                 | A    | recall    |       10 (5) |  -4.9 | -13.0 .. +8.1  | 15.0 |
| 6 vs 3                 | B    | avoids    |       15 (5) | -32.2 | -44.7 .. -10.0 | 24.8 |
| 6 vs 3                 | B    | satisfies |       15 (5) | -25.6 | -44.7 .. +10.0 | 39.1 |
| 6 vs 3                 | C    | passed    |       10 (5) |  +0.0 | +0.0 .. +0.0   |  0.0 |
| 6 vs 3                 | D    | precision |       13 (5) |  -6.5 | -17.6 .. +0.4  | 12.8 |
| 6 vs 3                 | D    | recall    |       13 (5) |  +2.2 | -7.7 .. +10.2  | 12.8 |

The n of a D precision comparison with arm 4 is 11: on two tasks no arm 4 answer made a
claim to grade.

| Arm | Injected tokens per task (mean, max) | Pre-edit calls | Pre-edit max | Answers' cost, 144 answers (USD) |
| --- | -----------------------------------: | -------------: | -----------: | -------------------------------: |
| 0   |                                 0, 0 |              0 |            0 |                            0.011 |
| 1   |                         1,878, 3,432 |              0 |            0 |                            0.062 |
| 2   |                           725, 1,015 |              0 |            0 |                            0.033 |
| 3   |                         1,190, 2,758 |            129 |          663 |                            0.035 |
| 4   |                           296, 1,048 |            129 |          273 |                            0.015 |
| 6   |                         1,049, 1,790 |            129 |          273 |                            0.038 |

Arm 1 injects 1.58 times arm 3's tokens. By ledger stage the run spent 0.194 USD on
answers, 0.391 on 1,728 judge passes and 0.004 on the server's own model and embedding
calls. For extraction this run's ledger charged an upper bound on the extractor it ran,
once per document before its extraction run, 0.173 USD in all: the extraction's model
calls cost at most 0.173, and its write embeddings are not recorded. For the
whole-document extractor the harness charges an estimate, not a bound
(`bench/design_rationale/README.md`). The ledger does not split spend by arm: the answers'
cost per arm is their recorded tokens priced at the current price table, and judging is
not split.

**Reading.** The results this run is about; a result is resolved when its interval clears
zero (the lower bound above it, or the upper bound below it), and the comparison table
holds the rest. The gold rows (arm 3) raise A and D precision and recall and B avoidance
and satisfaction over no design knowledge. The whole document (arm 1) raises D recall 10.5
points over the gold rows and lowers B avoidance below arm 0 (-14.4). Extracted rows alone
(arm 4) fall below the gold rows on A, B and D; adding the documents' knowledge hits to
them (arm 6) raises A precision (+31.1) and recall (+51.0) and D precision (+22.4) and
recall (+40.8) over arm 4, and D recall over the knowledge hits alone (arm 2, +8.0), and
leaves B avoidance unresolved against arm 4 (+1.1, -27.3 .. +38.9) and 32.2 points below
the gold rows. Arms 3 and 6 are not resolved apart on D recall (+2.2, -7.7 .. +10.2).
Unpaired, as per-arm rates: answers mislead more often with the documents alone than with
the gold rows, D misleading 47.4 in arm 2 and 23.1 in arm 1 against 16.7 in arm 3, and A
misleading 41.7 and 25.0 against 8.3; with the extracted rows, D answers mislead at about
the same rate with or without the documents (42.3 in arm 6, 37.2 in arm 4), and A answers
less often with them (26.7 against 36.7). A hypothesis, not tested here: B avoidance
follows the rows the pre-edit block serves. Arm 4's and arm 6's block served a row on 9 of
the 43 tasks with gold rows, arm 3's on 39, but the arms also differ in the rows
themselves, and no arm varies the block's reach alone.

**Treatment check.** Of the 43 tasks with gold rows, arm 3's pre-edit block served 39 and
search 42 (a task counts when it served one of its own gold rows); arm 4's and arm 6's
pre-edit block served 9 and search 33 (a task counts when it served any extracted row).
The knowledge hits held the task's own document on all 43 tasks with gold rows in arms 2
and 6; with five documents and five hits shown, this shows the hits were served, not how
well they ranked. Extraction wrote 19 rows across the documents and their revisions, 12
with files, with no extraction failure and no failed code lookup.

**Judge stability.** The headline verdict (A and D misleading, B avoids, C passed) agreed
across the two orders on 90.0% of 827 answers; an answer whose compared pass is
unreadable, and a change answer a detector decided, are left out. Each answer was judged
once per order, so agreement across repeats is not measured. 49 of 1,728 judge passes were
unreadable (13 answers unscored).

**Limits.** One reader and one judge model; no second judge and no human-labelled slice,
so judge accuracy and kappa are not measured. Extraction precision is not measured (no row
was labelled). No patch is applied and no test runs, so change-task pass rate is not
measured. The task set is private: the numbers are reproducible only by a holder of the
set.

- **Models:** reader, judge and extraction `gpt-6-luna`, in the summary's run flags; the
  extraction model is the LLM model the server settings of the harness's environment
  resolve (`LOOM_LLM_MODEL`), and `run` refuses an `--extract-model` that differs. The run
  does not record the server's own models, nor the revision of the code index arms 4 and
  6 looked their code names up in. The server ran from source on its own ClickHouse, with
  auth and background workers off.
- **Artifacts:** `bench/results/design_rationale.summary.json`, written by
  `score --summary`: the per-arm metrics, comparisons, injected tokens, treatment check,
  judge agreement, unreadable passes and the A and D answers with no claim, the task
  set's counts (documents, revisions, revised documents, tasks by kind and subtype, tasks
  with gold rows), the run's flags (arms, whether the extracted arms had files, models,
  window, K, judge orders and repeats) and its spend by ledger stage and the answers'
  cost per arm; the per-task rows, which name private tasks, are left out.
- **Reproduce:**
  `PYTHONPATH=$PWD .venv/bin/python -m bench.design_rationale run <taskset> --out <dir> --base-url <server> --arms 0,1,2,3,4,6 --reader-model gpt-6-luna --judge-model gpt-6-luna --code-index <url> --code-index-org <org> --code-index-namespace <code namespace> --extract-model gpt-6-luna --k 3 --judge-repeats 1 --max-usd 3`,
  then `... score <taskset> --out <dir> --summary bench/results/design_rationale.summary.json`.

### Document parsing

`bench/docparse/` scores the reference extractor on a committed corpus of the documents Loom
ingests: 54 PDF, DOCX and PPTX files (browser-printed and office-exported design docs,
typeset specs, two-column and rotated pages, borderless tables and tables over pages, slide
decks and their exports, scanned pages, a whiteboard photo, and six public-domain PEPs),
split into dev and held-out within each category. Gold labels come from the specs the files
were rendered from. Three systems, each through the same normalizer, each named by the stamp
its parses carry: the reference extractor at PDF parser version 1, DOCX 2 and PPTX 2 (text
layer: a PDF's text layer only, a DOCX's headings at their outline levels, no OCR); the
reference extractor at version 3 of each, with OCR on (layout and OCR: a PDF read as laid
out, a DOCX's headings under its title, Tesseract 5.5 reading scanned pages and large
pictures); and a layout-model baseline (a document parser that runs neural layout and table
models on CPU, whose version and settings the parses do not record). Every parser change was
tuned on dev; the parse scores below are held-out (27 files).

| Measure (held-out, 27 files)    | Text layer | Layout and OCR | Baseline |
| ------------------------------- | ---------: | -------------: | -------: |
| Text recall                     |      0.876 |          0.999 |    0.915 |
| Text precision                  |      0.880 |          0.984 |    0.961 |
| Heading F1                      |      0.342 |          0.962 |    0.957 |
| Heading level accuracy          |      0.700 |          1.000 |    0.357 |
| Table cell F1                   |      0.273 |          0.991 |    0.998 |
| Page and slide locators         |      0.814 |          0.991 |    0.875 |
| Speaker notes as notes (3 PPTX) |      1.000 |          1.000 |    0.000 |
| Silent losses (files)           |          5 |              2 |       10 |
| Seconds a file (mean)           |      0.053 |          0.520 |    9.016 |
| Peak memory a file (MB)         |         37 |             65 |    1,592 |

Heading level accuracy is taken over the files where a system matched a heading: 9 for the
text layer, 26 for the others. Silent loss is a file reported complete that dropped a block,
a page's text or a tenth of its words; it is a count, not a mean. The layout and OCR
reader's two are OCR misreads on files reported complete: a YAML code block on a scanned
page, and one label of a blurred whiteboard photo on a slide. Its text recall over the text
layer is +0.122 [+0.015, +0.264] and its text precision +0.105 [+0.001, +0.237] (paired, 95%
bootstrap over files); every metric's interval is in the artifact.

By category, the layout and OCR reader matches or beats the baseline on every measure above,
except:

- **Scanned pages and the whiteboard photo (3 files; headings and tables over the 2
  scans):** heading F1 0.505 against 0.909 and table cell F1 0.928 against 0.978. OCR's
  point sizes separate a title from body text but not one heading level from the next, and a
  scanned grid drawn in light gray is not always found. Text recall (0.997 against 0.649)
  and locators (0.983 against 0.625) are higher.
- **PDF exports (5 files):** table cell F1 0.991 against 1.000.
- **Tables over pages (2 files):** locators 0.977 against 1.000.
- **Slides and PPTX (6 files):** text precision 0.960 and 0.951 against 1.000, as text OCR
  reads in a slide's pictures holds words the gold does not; and a PPTX takes 0.39 s a file
  against 0.12 s, the cost of reading each large picture with OCR, which brings PPTX text
  recall to 1.000 (baseline 0.696).

Overall the layout and OCR reader is 17 times faster than the baseline and uses 4% of its
peak memory. Each system's time is its parse of a file, timed inside the process that parses
it, its start-up aside; the baseline's models also load in each such process (median 3.2 s,
`load_seconds` in its parses, over 54 files). The extractor image is 511 MB with OCR and 467
MB without (OCR adds 44 MB unpacked); the baseline's image is 3.46 GB
(`bench/results/docparse_images.json`, `docker image ls` on each build).

**Downstream.** Each system's artifacts are ingested into a running Loom through a stub
extractor (the fake n-gram embedder and the fake LLM provider,
`docparse_models_downstream.json`), and up to three paragraphs and two table rows of every
file in both splits (212 queries) go to `knowledge.search` within their own document and
system's namespace. Found: a top-5 hit holds 70% of the answer's words; cited: that hit's
locator holds the answer's page. Ingests that failed score zero: the text layer's four scans
(no text without OCR). The whiteboard photo, whose ingest failed for the text layer and the
baseline, holds no query. The text layer and layout and OCR are served their extractor's own
artifacts, the baseline the artifacts `artifacts` builds from its normalized parses.

| Measure                           | Text layer | Layout and OCR | Baseline |
| --------------------------------- | ---------: | -------------: | -------: |
| Cited at 1 (212)                  |      0.792 |          0.825 |    0.858 |
| Cited at 5 (212)                  |      0.887 |          0.943 |    0.948 |
| Found at 5 (212)                  |      0.920 |          0.976 |    1.000 |
| Cited at 5 (104 held-out queries) |      0.856 |          0.923 |    0.952 |

Against the text layer, layout and OCR is +0.057 [-0.029, +0.160] cited at 5 and +0.057
[-0.018, +0.149] found at 5 over all 212 queries; neither interval excludes zero. Scanned
pages go from 0 to 1.00 cited at 5. It loses significantly on rotated pages (cited at 1,
-0.30 [-0.40, -0.20]), borderless tables (cited at 1, -0.15 [-0.20, -0.05]) and tables over
pages (cited and found at 5, -0.20 [-0.35, -0.05]), and on tables over pages it trails the
baseline (cited at 1 0.60 against 0.95, found at 5 0.80 against 1.00). It is the only system
that misses queries whose file it ingested: 5 found in no top-5 hit, four rows of long
tables and one row of a two-column page's table. Where it loses at 5, a row is either found
and not cited, because the chunk that holds a table's first rows also holds the paragraph
before the table and so starts on the page before (the scorer cites a row only within its
table's pages), or not found, as `knowledge.search` folds the row blocks of one section and
a row deep in a long table falls out of the top five. At 1 it also loses some paragraph
queries of PDF exports to rank.

**Decision extraction.** The twelve PDFs of `DECISION_FILES` (six PEPs and six design docs,
from both splits) are mined in a dry run from each system's ingested text with `gpt-6-luna`,
and counted where extraction checks quotes against the parsed text:

| Measure (12 documents)                  | Text layer | Layout and OCR | Baseline |
| --------------------------------------- | ---------: | -------------: | -------: |
| Decisions, reasons and rejected options |         66 |             84 |       62 |
| Of them, quotes in the parsed text      |      0.985 |          1.000 |    0.984 |
| Kept quotes found in the gold text      |      1.000 |          1.000 |    0.971 |

One run of one model; the first count moves from run to run, so only the two rates are
comparable across systems.

- **Models:** no LLM or embedder for the parse scores (layout and OCR runs Tesseract 5.5,
  stamped in its parses; the baseline its own layout and table models); the downstream run
  used the fake n-gram embedder and the fake LLM provider
  (`bench/results/docparse_models_downstream.json`); decision extraction used `gpt-6-luna`
  (`bench/results/docparse_models_decisions.json`).
- **Artifacts:** `bench/results/docparse_parses/` (each system's normalized parse of every
  file: `text_layer.jsonl`, `layout_ocr.jsonl`, `baseline.jsonl`),
  `bench/results/docparse_heldout.json` (every metric per category with paired intervals,
  and every file's scores), `bench/results/docparse_downstream.json` (the `downstream`
  harness's output: ingest states, the query set with each answer, each query's judgement,
  the paired report by category and by split, and the decision counts; its systems renamed
  from the run's names, its queries given their split from `gold.jsonl`, and its report
  recomputed with the split groups). The baseline's time for two held-out files regenerated
  after its run (typeset_1, scan_2) is from their earlier versions, as its re-run shared the
  machine with other work; their quality scores are from the re-run.
- **Reproduce:** the parse scores from the committed parses, `PYTHONPATH=$PWD
.venv/bin/python -m bench.docparse score
text_layer=bench/results/docparse_parses/text_layer.jsonl
layout_ocr=bench/results/docparse_parses/layout_ocr.jsonl
baseline=bench/results/docparse_parses/baseline.jsonl --split heldout --json out.json`;
  the reference extractor's parses with `run --out layout_ocr.jsonl --artifacts
art/layout_ocr`, inside the extractor image for OCR (a tree whose PDF, DOCX and PPTX
  parsers are at versions 1, 2 and 2 gives the text layer's); the baseline's column is
  re-scored from its committed parses, not regenerated here; the baseline's artifacts,
  `PYTHONPATH=$PWD .venv/bin/python -m bench.docparse artifacts
bench/results/docparse_parses/baseline.jsonl --out art/baseline`; downstream,
  `PYTHONPATH=$PWD .venv/bin/python -m bench.docparse downstream text_layer=art/text_layer
layout_ocr=art/layout_ocr baseline=art/baseline --base-url <loom> --token <the stub's
token> --out downstream.json`, and with `--decisions 12` and the server's settings for a
  real LLM for the decision counts, against a Loom whose document extractor is the stub
  (`python -m bench.docparse --help`).

## Reference computations

Deterministic outputs of the scoring code on fixed input, pinned by unit tests. The
methodology rests on them; they do not measure Loom.

### Lexical scoring reference values

`bench/answer_scoring.py` reproduces the published lexical tokenisation. A unit test pins
its output on one item (gold `7 May 2023`):

| Prediction                                                 | Token F1 | BLEU-1 |
| ---------------------------------------------------------- | -------- | ------ |
| `Caroline went to the LGBTQ support group on May 7, 2023.` | 0.3750   | 0.2308 |
| `May 7, 2023` (simplified)                                 | 0.8571   | 0.7500 |
| `8 May 2023` (wrong)                                       | 0.6667   | 0.6667 |

The wrong answer outscores the correct sentence, so lexical columns are computed only
after simplification and never replace the judge.

```bash
.venv/bin/python -m pytest tests/unit/test_answer_scoring.py -q
.venv/bin/python -c "from bench.answer_scoring import token_f1, bleu1
for p in ['Caroline went to the LGBTQ support group on May 7, 2023.', 'May 7, 2023', '8 May 2023']:
    print(round(token_f1(p, '7 May 2023'), 4), round(bleu1(p, '7 May 2023'), 4), p)"
```

### Bootstrap intervals on fixed deltas

The interval widths behind the noise floor: see
[methodology](./methodology.md#how-wide-the-interval-is) for the table and command.

### Majority voting on simulated reader noise

`test_majority_voting_narrows_the_null_band_on_synthetic_reader_noise` in
`tests/unit/test_bench_gate.py` simulates a reader that flips each label with a fixed
probability on 400 items. It asserts that a three-repeat majority narrows a
same-configuration band and more than halves between-arm discordance. This is why gates
use `--repeats 3`.

```bash
.venv/bin/python -m pytest tests/unit/test_bench_gate.py tests/unit/test_bench_bootstrap.py -q
```

## Scorer self-checks

Each runs offline on committed fixtures and exits non-zero when the scorer miscounts. The
printed scores are the ceiling of a perfect synthetic retrieval, not Loom's.

| Command                                                       | Asserts                                                                                                   |
| ------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `make bench-code-intel-dry`                                   | Every lane of `code_intel_qa.jsonl` scores its ceiling under a perfect retrieval                          |
| `.venv/bin/python bench/coding_agent_qa.py --dry`             | R@5, R@10, MRR and fix-reuse compute on the committed lesson set                                          |
| `.venv/bin/python bench/code_plane_ablation.py selftest`      | The ablation scorer gives the expected scores on the arm A / arm C transcripts                            |
| `.venv/bin/python bench/acceptance/code_agent.py --dry`       | The acceptance report passes on synthetic retrievals                                                      |
| `.venv/bin/python -m pytest bench -q`                         | Every harness's scoring core and dataset reader (also run by `make test-unit` and CI)                     |
| `.venv/bin/python -m pytest tests/unit/test_skill_eval.py -q` | The bad replay fails with the expected finding codes, the good one passes ([skill eval](./skill-eval.md)) |

## What is not committed

- **Gate thresholds are not results.** The 0.80 quality floor and the `bench-code-intel`
  floors in the `Makefile` say what a build must clear, not what any build scored.
- **Fixture headers record gate history.** Some JSONL headers (for example
  `code_intel_qa_multilang.jsonl`) state the baseline a gate was accepted at. Reproducing
  it needs a live index of the same repository, so this page does not restate it.
- **Harness docstrings mention measurements** (the null band, flip rates, acceptance
  floors). They explain a default; they are not reproducible from committed artifacts. The
  code's defaults (`DEFAULT_BAND_PP`, `GATE_REPEATS`) are the operative part.
- **Per-run outputs** stay local ([where results land](./running.md#where-results-land)).
- **Consumer replays** need data outside this repository.

## Adding a result

Commit the artifact the harness writes, named after the benchmark, with the models it ran
under (`bench/models_used.py`: embedder and server LLMs, plus the bench's reader and judge)
and a reproduction command. Add both a row to the
[protocol index](#published-benchmark-protocol-index) and a subsection under
[measurements](#measurements). The index row must state:

- the exact task presented to Loom, including anything the evaluator withholds;
- the public corpus, selection method, random seed and number of questions or operations;
- the Loom operation, indexed scope, search mode and retrieval depth;
- the gold label, scoring rule, reader, judge and baseline;
- whether the run measures retrieval, answer quality, an agent outcome or efficiency.

The detailed subsection must link the per-example artifact, model manifest and exact
command, and must report failures, exclusions, uncertainty and material negative results.
A number without this evidence does not go here or in the README.

## Related

- [Running](./running.md) and [methodology](./methodology.md).
- [Code index](../features/code-index/overview.md): where `code.ask` and its cache fit.
