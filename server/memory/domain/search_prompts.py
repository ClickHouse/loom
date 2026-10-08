"""LLM prompt constants for the memory-search pipeline."""

from __future__ import annotations

_DERIVED_INTENT_PROMPT = """You map a COUNT / SUM / DURATION question to a structured
aggregate over per-occurrence event rows. The rows have columns:
subject, predicate (a canonical verb), object, category, numeric_value,
unit, occurred_at. Your job is to say HOW to aggregate them.

Return JSON:

  "op": ONE of:
    - "count"     — how many times something happened / how many distinct
                    things ("how many bikes did I buy", "how many breaks")
    - "sum"       — total of an amount/quantity/duration ("total spent on
                    luxury items", "how much did I spend on gear")
    - "date_span" — duration / interval / ordering between the first and
                    last occurrence ("how long between my first and last
                    doctor visit", "how many weeks since I started X")

  "subject": the holder of the events, lowercase. Almost always "user"
    (the person whose memory this is). Use a named person only when the
    question is explicitly about someone else.

  "predicate": the canonical base verb of the recurring action — the verb
    the events were logged under. Lowercase, singular/base form: "buy",
    "spend", "visit", "attend", "consult", "take". "" if the question has
    no clear action verb (a pure category count).

  "category": the scope noun-phrase that filters which occurrences count,
    lowercase singular ("bike gear", "doctor", "luxury item", "social
    media break"). "" if the question counts every occurrence of the verb
    regardless of kind.

  "object": a SPECIFIC named object to filter on, lowercase, or "" when
    the question is about a category rather than one named thing.

Only emit an intent when the question genuinely needs cross-occurrence
aggregation. If it asks for a single stored fact, return {"op":""}.

Examples:

Q: "How many bikes have I bought?"
A: {"op":"count","subject":"user","predicate":"buy","category":"bike","object":""}

Q: "How much have I spent on bike gear in total?"
A: {"op":"sum","subject":"user","predicate":"spend","category":"bike gear","object":""}

Q: "How long was it between my first and last visit to the dermatologist?"
A: {"op":"date_span","subject":"user","predicate":"visit","category":"dermatologist","object":""}

Q: "How many days did I take social media breaks?"
A: {"op":"count","subject":"user","predicate":"take","category":"social media break","object":""}

Q: "What is my favorite movie?"
A: {"op":""}

Return ONLY the JSON. No prose, no markdown fences."""


_ITERATIVE_JUDGE_PROMPT = """You are evaluating whether retrieved memory excerpts
answer a user's question. Return JSON:

{
  "answered": boolean,
  "suggested_query": string  // empty if answered=true
}

answered=true when the excerpts contain (directly OR through obvious
implication) the information needed to answer the user's question.

answered=false when:
  - excerpts are about a related-but-different topic
  - excerpts mention the subject but not the asked-about attribute
  - excerpts are too thin to construct an answer

suggested_query: when answered=false, ONE alternate phrasing of the
user's question that might retrieve the right memory. Use the user's
likely vocabulary; preserve named entities; rephrase verbs.

Return ONLY the JSON, no prose."""


_LLM_TRIPLE_PROMPT = """Extract subject-relation-object triples from the search query that
the answer would confirm. Return ONLY a JSON array, no prose.

Each triple is {"s": subject, "v": relation_verb, "o": object}.
- Subject/object: lowercase entities. Use "?" when the query is asking for that slot.
- Verb: 1-3 lowercase words, underscore-joined. Use whatever verb the query uses
  — don't force into any canonical list. The stemmer handles inflection.

Extract every (s, v, o) triple stated or strongly implied by the query — including
the query subject itself as `s` when the verb is intransitive. Return [] only when
the query has no recognizable verb/entity structure (greetings, fragments).

Examples (format anchors):
  "how much does X weigh"        → [{"s":"x","v":"weighs","o":"?"}]
  "where does Y work"            → [{"s":"y","v":"works_at","o":"?"}]
  "hello"                        → []
"""


_VERB_SYNONYM_PROMPT = """Return verbs synonymous with the given verb, for matching against
a typed-relation store. ONLY a JSON array of lowercase verb strings,
no prose, no explanation.

Size: between 3 and 6 entries. Fewer for verbs with few natural
paraphrases; more for action verbs with many synonyms.

Include: tense/aspect variants, common paraphrases, the original verb itself.
Preserve the input's shape — if the input uses underscores for multi-word
verbs ("lives_in"), emit the same shape ("resides_in").

If the verb is unusual or has no good synonyms, return just the verb itself
as a single-entry array."""


# HyDE: write a plausible ANSWER, embed that, and retrieve against it. Versioned
# like every other prompt; it is the most frequently executed LLM leg in the
# search path.
_HYDE_PROMPT = (
    "Write the most likely sentence-form ANSWER to a "
    "question, as if you already had the relevant memory. "
    "Do NOT say 'I don't know' — guess the most likely "
    "answer-shape. The output is used to retrieve the "
    "real memory by embedding similarity, so the SHAPE "
    "(vocabulary, named entities) matters more than "
    "correctness. Output one short sentence."
)
