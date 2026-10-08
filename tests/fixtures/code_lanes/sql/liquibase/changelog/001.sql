--liquibase formatted sql

--changeset alice:1
CREATE TABLE t (id int);
--rollback DROP TABLE t;

--changeset bob:2
ALTER TABLE t ADD COLUMN name text;
