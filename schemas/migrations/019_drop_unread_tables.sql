-- Two tables nothing reads or writes. loom.entity_triple_embeddings lost its
-- only reader with facts.search; loom.session_outcomes (keyed by session id
-- alone) was superseded by loom.session_outcomes_by_user, which holds every
-- session it judged.
DROP TABLE IF EXISTS loom.entity_triple_embeddings;

DROP TABLE IF EXISTS loom.session_outcomes;
