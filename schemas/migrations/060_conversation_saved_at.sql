-- Save time is independent of the original transcript timestamp.
-- Existing saves use their recorded message time.
ALTER TABLE loom.conversations
    ADD COLUMN IF NOT EXISTS saved_at Nullable(DateTime64(3, 'UTC'))
    DEFAULT if(is_saved, ts, NULL);
ALTER TABLE loom.conversation_embeddings
    ADD COLUMN IF NOT EXISTS saved_at Nullable(DateTime64(3, 'UTC'))
    DEFAULT if(is_saved, ts, NULL);
