-- The function behind a cell's `loom.tenancy` guard (docs/architecture/cells.md, "Writers
-- older than the check are refused by the stores they write"). It adds no trigger: a cell's
-- boot creates `tenancy_record` on an `enforced` cell, and attach once it sets `enforced`
-- (`ensure` in server/core/cell_triggers.py); on a deployment that is not a cell no trigger
-- calls it.
--
-- A tenancy row makes an org held. Only a session that ran the tenancy check writes one
-- (`loom.tenancy_checked = 'on'`, which the trigger's WHEN skips); a row inserted, or moved
-- to another org, by any other session is refused, SQLSTATE LT001, in every state but a
-- first cell still recording. `open` is refused too: the row itself would make the state
-- `partial`. Without it, a script could make an org held in a cell with no placement in the
-- router's directory, where a later cell could place it too.
CREATE OR REPLACE FUNCTION loom.tenancy_refuse_unmarked() RETURNS trigger
    LANGUAGE plpgsql
    VOLATILE
    SECURITY INVOKER
    SET search_path = pg_catalog, loom, pg_temp
AS $$
DECLARE
    first_is boolean;  -- NULL when there is no `first` row
    mode_now text;
    has_own boolean;
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.org = NEW.org COLLATE "C" THEN
        RETURN NULL;  -- an update that keeps the row's org
    END IF;
    SELECT f.is_first INTO first_is FROM loom.cell_first f;
    SELECT m.mode INTO mode_now FROM loom.tenancy_mode m;
    has_own := EXISTS (SELECT 1 FROM loom.cell_self);
    IF first_is AND mode_now = 'recording' AND has_own THEN
        RETURN NULL;  -- a first cell still recording
    END IF;
    RAISE EXCEPTION
        'cell refuses a tenancy row for org % from a session that ran no tenancy check',
        NEW.org
        USING ERRCODE = 'LT001', TABLE = TG_TABLE_NAME, SCHEMA = TG_TABLE_SCHEMA,
              HINT = 'run a current Loom with LOOM_CELL set';
END;
$$;
