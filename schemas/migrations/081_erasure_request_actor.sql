-- The audit actor of the request, as `begin_erasure` records it: `writer_actor`'s
-- value (the token's `kind:subject`, else the bound pipeline actor, else the
-- body's requester), '-' when that is empty. The saga's started, failed and
-- completed audit rows name it, on a resumed run too; `requester` stays what the
-- body declares. A row written before this file, or checkpointed since by a build
-- before it, reads '', and its resumed saga names the process that resumes it.
ALTER TABLE loom.erasure_requests
    ADD COLUMN IF NOT EXISTS `actor` String DEFAULT '';
