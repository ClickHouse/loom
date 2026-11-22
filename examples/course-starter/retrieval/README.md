# Retrieval failure worksheet

Practice passage retrieval and answerability decisions with a synthetic document and
labeled questions. This extends [Lab 2](../README.md#the-labs) on the same local stack,
without generated answers or a model API key.

## Prepare the exercise

Complete the existing Lab 2 ingest and search TODOs first. Preserve any work in
`labs/notes.md`, then copy this folder's `notes.md` there. The document describes a
fictional workshop service. Its settings and policies are exercise data.

Use `development-labels.jsonl` to select a similarity cutoff and
`evaluation-labels.jsonl` to practice applying it to other questions. Both files are
public. For an assessment, the instructor prepares different private evaluation
questions and releases them only after the cutoff and configuration are fixed. The
supplied evaluation questions do not support a claim about unseen test data.

Use the [course guide](https://github.com/ClickHouse/loom/blob/main/docs/guides/teaching.md#lab-2-document-search-with-citations)
for setup and SDK references. Use synthetic data on your own local stack; hosted
compute and storage have their own billing. The starter's existing grader checks the
original lab behavior. The written analysis below needs instructor assessment.

## Run the comparison

1. Record the source commit, installed image digest or release, SDK version,
   embedding and chunking settings, namespace and search mode. Keep commands and
   configuration without credentials. The source starter and installed release can
   differ; record both.
2. Ingest the notes through the completed Lab 2 function. Preserve the document
   identity and `lab2.SOURCE_URI`. Search with `uri_prefix=[lab2.SOURCE_URI]` and
   `top_k=3` for every question. Check that every hit belongs to this document: a URI
   prefix can match other documents. If it does, give the exercise a unique source
   URI and ingest again before fixing the configuration. A local stack may already
   contain documents from other labs.
3. Retain the full responses, including hit text, document identity, `similarity`
   and `score` where returned. On development questions, use the labeled section
   and evidence substring to identify relevant hits. Inspect the source passage
   when an excerpt cuts through the answer. Agree on the relevance rule before
   grading; chunk identifiers and boundaries can change.
4. Calculate recall@3 as answerable questions with at least one relevant hit in the
   first three results divided by all answerable questions. A hit from the right
   document but the wrong section is not relevant. This worksheet gives each
   answerable question one relevant section.
5. Select a cutoff using development questions only. Predict answerable when a hit
   exists and the greatest returned `similarity` is greater than or equal to the
   cutoff. No hits predicts unanswerable. A missing or nonnumeric similarity is an
   invalid measurement to investigate. Use similarity rather than the hybrid
   fused `score` for this decision.
6. Fix the cutoff, source scope, search mode and k before opening the evaluation
   questions. Run them with those settings and keep the original responses.
   Report recall@3 and an answerability confusion matrix separately. A false
   positive predicts answerable for an unanswerable question; a false negative
   predicts unanswerable for an answerable question. Give counts and denominators.
7. Explain an incorrect retrieved passage and a cutoff mistake if observed. If
   none occur, report that. Discuss the small sample, repeated subject matter and
   sensitivity to embedding and chunking settings. The questions share one
   document, so the result does not establish retrieval quality on unseen corpora.

## Record the results

| Question ID                                 | Answerable label | Relevant section in top 3 | Maximum similarity or no hits | Fixed cutoff | Predicted answerable | Explanation |
| ------------------------------------------- | ---------------- | ------------------------- | ----------------------------- | ------------ | -------------------- | ----------- |
| Fill from the original responses and labels |                  |                           |                               |              |                      |             |

For an arithmetic example, three relevant top-three hits among four answerable
questions give recall@3 of 3/4. If one of two unanswerable questions crosses the
cutoff, the false-positive fraction is 1/2. These are example counts, not measured
Loom results.

## Assess the work

| Dimension                   | Points | Evidence                                                                                               |
| --------------------------- | ------ | ------------------------------------------------------------------------------------------------------ |
| Reproducibility             | 4      | Source, release and configuration receipt; fixed k and source scope; commands and original responses   |
| Labels and metrics          | 4      | Passage relevance, correct denominators, a fixed cutoff and the evaluation confusion matrix            |
| Failure analysis and limits | 2      | Inspectable observed cases, no invented failures and a clear account of the small same-document sample |

Keep the fixture, labels and responses with the submission. The starter's license
and notices apply to these original synthetic materials too. A reproducible defect
can follow the repository's existing contribution path after checking current
issues and active work; an assignment does not reserve upstream work.
