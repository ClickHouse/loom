-- Lifecycle cleanup identifies a connection without decoding its private payload.
ALTER TABLE loom.knowledge_sources ADD COLUMN IF NOT EXISTS connection_slot String DEFAULT '';
ALTER TABLE loom.knowledge_sources ADD COLUMN IF NOT EXISTS source_contract_version UInt16 DEFAULT 0;
