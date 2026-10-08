-- Deploy flipr:users to pg
BEGIN;
CREATE TABLE users (nick text);
COMMIT;
