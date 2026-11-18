-- Coarse keys reduce candidates; the checked leg always parses the current list.
-- Unicode buckets keep every Python-normalized identity in the candidate stream.
ALTER TABLE loom.documents ADD COLUMN IF NOT EXISTS access_keys Array(String)
MATERIALIZED arrayDistinct(arrayConcat(
    arrayMap(x -> concat('e:', lowerUTF8(trimBoth(x, char(9,10,11,12,13,28,29,30,31,32)))), JSONExtract(access_json, 'emails', 'Array(String)')),
    arrayMap(x -> concat('d:', lowerUTF8(trimBoth(x, char(9,10,11,12,13,28,29,30,31,32)))), JSONExtract(access_json, 'domains', 'Array(String)')),
    if(JSONExtractString(access_json, 'owner') != '', ['owner'], []),
    if(notEmpty(JSONExtract(access_json, 'groups', 'Array(String)')), ['group'], []),
    if(JSONExtractBool(access_json, 'anyone'), ['anyone'], []),
    if(arrayExists(x -> NOT isASCII(x), JSONExtract(access_json, 'emails', 'Array(String)')), ['email_unicode'], []),
    if(arrayExists(x -> NOT isASCII(x), JSONExtract(access_json, 'domains', 'Array(String)')), ['domain_unicode'], [])
));

ALTER TABLE loom.documents ADD INDEX IF NOT EXISTS access_keys_bloom access_keys TYPE bloom_filter(0.01) GRANULARITY 4;

-- Keep each insert trigger and target while narrowing its query in place.
ALTER TABLE loom.chunk_term_freq_mv MODIFY QUERY
SELECT
    org,
    namespace,
    term,
    uniqExactState((source_uri, chunk_idx)) AS df_state,
    now() AS df_added_at
FROM loom.chunks
ARRAY JOIN arrayDistinct(arrayFilter(t -> (length(t) >= 3), arrayMap(t -> lowerUTF8(t), splitByNonAlpha(content)))) AS term
WHERE (content != '') AND (doc_superseded_at IS NULL)
    AND NOT match(namespace, '^acl-[0-9a-f]{24}$')
GROUP BY org, namespace, term;

ALTER TABLE loom.chunk_context_term_freq_mv MODIFY QUERY
SELECT
    org,
    namespace,
    term,
    uniqExactState((source_uri, chunk_idx)) AS df_state,
    now() AS df_added_at
FROM loom.chunks
ARRAY JOIN arrayDistinct(arrayFilter(t -> (length(t) >= 3), arrayMap(t -> lowerUTF8(t), splitByNonAlpha(context)))) AS term
WHERE (content != '') AND (context != '') AND (doc_superseded_at IS NULL)
    AND NOT match(namespace, '^acl-[0-9a-f]{24}$')
GROUP BY org, namespace, term;
