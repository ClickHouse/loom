CREATE TABLE orders (
    order_id SERIAL PRIMARY KEY,
    region TEXT NOT NULL,
    amount NUMERIC(12, 2),
    placed_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX orders_region_idx ON orders USING btree (region);

INSERT INTO orders (region, amount) VALUES ('emea', 10.50) RETURNING order_id;

DELETE FROM orders WHERE placed_at < now() - INTERVAL '2 years';
