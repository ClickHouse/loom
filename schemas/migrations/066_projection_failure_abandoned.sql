-- A projection failure the repair gave up on: no retry can do its work (a
-- supersession chain that loops, or runs past the repair's reading budget). It
-- leaves the active queue, as a resolved one does, but stays apart from the work
-- that was done, with the reason in `error_message`, for an operator to look at.
ALTER TABLE loom.memory_projection_failures
    MODIFY COLUMN `status` Enum8('active' = 1, 'resolved' = 2, 'abandoned' = 3) DEFAULT 'active';
