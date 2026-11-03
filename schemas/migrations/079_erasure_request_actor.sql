-- The principal the request's token named, as `kind:subject`, so the audit rows
-- of a saga name it, a resumed one's included. `requester` stays what the body
-- declares. A row written before this file reads '', and the audit rows of its
-- resumed saga name the process that resumes it.
ALTER TABLE loom.erasure_requests
    ADD COLUMN IF NOT EXISTS `actor` String DEFAULT '';
