-- The function behind a cell's org triggers (docs/architecture/cells.md, "Writers older
-- than the check are refused by the stores they write"). It adds no trigger: a cell's
-- boot creates them on a fully prepared cell in `enforced` (server/core/cell_triggers.py),
-- and on a deployment that is not a cell no trigger calls it.
--
-- It applies the cell state rule (server/core/cell.py, `state`): with no state row
-- (`open`) or a first cell still recording, a write passes; in any other state a row
-- for an org with no loom.tenancy row is refused, SQLSTATE LT001. The sentinel orgs
-- ('', '-', '*') pass. A session that ran the tenancy check marks itself
-- (`loom.tenancy_checked = 'on'`), and the triggers' WHEN skips it.
CREATE OR REPLACE FUNCTION loom.tenancy_refuse_unheld() RETURNS trigger
    LANGUAGE plpgsql
    VOLATILE
    SECURITY INVOKER
    SET search_path = pg_catalog, loom, pg_temp
AS $$
DECLARE
    first_is boolean;  -- NULL when there is no `first` row
    mode_now text;
    has_own boolean;
    has_tenancy boolean;
BEGIN
    SELECT f.is_first INTO first_is FROM loom.cell_first f;
    SELECT m.mode INTO mode_now FROM loom.tenancy_mode m;
    has_own := EXISTS (SELECT 1 FROM loom.cell_self);
    has_tenancy := EXISTS (SELECT 1 FROM loom.tenancy);
    IF first_is IS NULL AND mode_now IS NULL AND NOT has_own AND NOT has_tenancy THEN
        RETURN NULL;  -- open: a deployment that is not a cell
    END IF;
    IF first_is AND mode_now = 'recording' AND has_own THEN
        RETURN NULL;  -- a first cell still recording
    END IF;
    IF NEW.org IS NULL THEN
        RAISE EXCEPTION 'cell refuses a row with no org'
            USING ERRCODE = 'LT001', TABLE = TG_TABLE_NAME, SCHEMA = TG_TABLE_SCHEMA,
                  HINT = 'run a current Loom with LOOM_CELL set';
    END IF;
    IF NEW.org IN ('', '-', '*') THEN
        RETURN NULL;
    END IF;
    IF EXISTS (SELECT 1 FROM loom.tenancy t WHERE t.org = NEW.org COLLATE "C") THEN
        RETURN NULL;
    END IF;
    RAISE EXCEPTION 'cell holds no tenancy row for org %', NEW.org
        USING ERRCODE = 'LT001', TABLE = TG_TABLE_NAME, SCHEMA = TG_TABLE_SCHEMA,
              HINT = 'run a current Loom with LOOM_CELL set';
END;
$$;
