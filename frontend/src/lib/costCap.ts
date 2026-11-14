/** What a namespace's daily cost cap stops once its rolling 24h spend reaches it, as the server
 *  applies it: entity extraction and code summaries wait
 *  (server/memory/workers/entity_extractor.py, server/code/workers/summary.py), code indexed
 *  meanwhile is stored without descriptions (server/code/app/index/pass_stream.py), the
 *  Library's unanswered questions keep their last build
 *  (server/knowledge/workers/knowledge_gaps.py), code search skips query expansion
 *  (server/code/app/search/handler.py), memory and code questions answer without synthesis
 *  (server/memory/app/ask/handler.py, server/code/app/ask.py) and document questions abstain
 *  (server/knowledge/app/synthesize.py). The assistant, Library decision extraction and
 *  conflict resolution stop earlier, at their own share of the cap
 *  (server/assistant/app/bridge.py `assistant_budget_share`;
 *  server/knowledge/app/source_sync.py and server/memory/workers/conflict_resolver.py
 *  `*_cap_share`). Other model calls are not capped, so the sentence lists what stops rather
 *  than saying every call does. Writes are accepted. Shown in the namespace settings and on
 *  Usage. */
export const COST_CAP_EFFECT =
  "Once the cap is reached, entity extraction and code summaries wait, code indexed meanwhile is stored without descriptions, the Library's unanswered questions stop updating, code search stops expanding queries, questions about memory and code return their sources without a written answer, and questions over documents are declined, until spend drops below the cap. The assistant, Library decision extraction and conflict resolution stop earlier, once each has used its share of the cap. Writes are still accepted.";
