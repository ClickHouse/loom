CREATE SEQUENCE IF NOT EXISTS loom.knowledge_source_refresh_fence
    AS bigint MAXVALUE 9007199254740991 NO CYCLE;

CREATE TABLE IF NOT EXISTS loom.knowledge_source_refresh (
    org text COLLATE "C" NOT NULL,
    namespace text COLLATE "C" NOT NULL,
    agent text COLLATE "C" NOT NULL,
    user_id text COLLATE "C" NOT NULL,
    session_id text COLLATE "C" NOT NULL,
    item_key text COLLATE "C" NOT NULL,
    actor text NOT NULL,
    source_class text NOT NULL,
    worker_id uuid NOT NULL,
    source_uri text COLLATE "C" NOT NULL,
    state text NOT NULL CHECK (state IN ('pending', 'claimed', 'completed', 'exhausted', 'cancelled')),
    fence bigint NOT NULL DEFAULT 0 CHECK (fence BETWEEN 0 AND 9007199254740991),
    attempts smallint NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 8),
    claimable_at timestamptz NOT NULL,
    request_sha256 text NOT NULL,
    task jsonb NOT NULL,
    receipt jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((state = 'completed') = (receipt IS NOT NULL)),
    PRIMARY KEY (org, namespace, item_key)
);

CREATE INDEX IF NOT EXISTS knowledge_source_refresh_claimable
    ON loom.knowledge_source_refresh (org, namespace, worker_id, claimable_at, item_key)
    WHERE state IN ('pending', 'claimed');

CREATE INDEX IF NOT EXISTS knowledge_source_refresh_by_user
    ON loom.knowledge_source_refresh (org, user_id, namespace);
