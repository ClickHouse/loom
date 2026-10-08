-- migrate:up
CREATE TABLE users (id int);
-- migrate:down
DROP TABLE users;
