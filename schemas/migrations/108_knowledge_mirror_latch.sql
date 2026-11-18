-- Deployment privacy state survives process switches and tenant erasure.
CREATE TABLE IF NOT EXISTS loom.knowledge_mirror_latch
(
    id UInt8,
    set_at DateTime64(3),
    CONSTRAINT one_deployment CHECK id = 0
)
ENGINE = KeeperMap('/loom/knowledge_mirror_latch')
PRIMARY KEY id;
