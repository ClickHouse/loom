CREATE TABLE IF NOT EXISTS loom.memory_refusals (
    org text COLLATE "C" NOT NULL,
    namespace text COLLATE "C" NOT NULL,
    agent text COLLATE "C" NOT NULL DEFAULT '-',
    user_id text COLLATE "C" NOT NULL DEFAULT '-',
    session_id text COLLATE "C" NOT NULL DEFAULT '-',
    scope_kind text NOT NULL DEFAULT 'identity',
    scope_value text NOT NULL,
    actor text NOT NULL,
    source_class text NOT NULL DEFAULT 'derived',
    kind text NOT NULL,
    units integer NOT NULL,
    digest text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org, scope_kind, scope_value, user_id, kind, units, digest)
);
