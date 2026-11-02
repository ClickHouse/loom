-- Where an original's bytes are: '' for the `data` column of this row, or the
-- `s3://<bucket>/<key>` of the object holding them (LOOM_DOCUMENT_OBJECT_STORE=s3),
-- the row then keeping `data` empty as the object's catalog entry. A build that
-- does not know the column reads the empty `data` and answers such a download
-- 410, so the S3 store is switched on once every replica reads `location`.
ALTER TABLE loom.document_blobs
    ADD COLUMN IF NOT EXISTS `location` String DEFAULT '';
