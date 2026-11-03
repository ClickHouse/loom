-- The principal the request's token named, as `kind:subject`, so every audit row
-- of a saga names it, a resumed one included. `requester` stays what the body
-- declares. A row written before this file reads '' and its resumed saga's
-- audit rows name the recovery worker.
ALTER TABLE loom.erasure_requests
    ADD COLUMN IF NOT EXISTS `actor` String DEFAULT '';
