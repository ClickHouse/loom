-- A collection's title: what a person typed when creating it, shown in place of
-- its `col-` name. Titles may repeat; the name stays the address
-- (server/identity/app/collections.py). A row written before this file, or by a
-- build before it, reads '' and is shown by its name.
ALTER TABLE loom.namespaces
    ADD COLUMN IF NOT EXISTS `title` String DEFAULT '' AFTER `description`;
