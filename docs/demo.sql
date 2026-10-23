-- The demo database of the README: a small shop the agent may read and an hr schema it may not.
-- Run it in an empty database (scripts/demo.py creates one and then runs this file). The data is
-- made with arithmetic, not random(), so every copy answers the same: 50 customers, 20 products,
-- 500 orders (174 shipped, 162 paid, 90 cancelled, 74 new) and 2200 order items.

DROP SCHEMA IF EXISTS shop CASCADE;
DROP SCHEMA IF EXISTS hr CASCADE;

CREATE SCHEMA shop;
CREATE SCHEMA hr;

CREATE TABLE shop.customers (
    id integer PRIMARY KEY,
    full_name text NOT NULL,
    email text NOT NULL UNIQUE,
    city text,
    created_at timestamp with time zone NOT NULL DEFAULT now()
);
COMMENT ON TABLE shop.customers IS 'Покупатели';
COMMENT ON COLUMN shop.customers.city IS 'Город доставки по умолчанию';

CREATE TABLE shop.products (
    id integer PRIMARY KEY,
    name text NOT NULL,
    category text NOT NULL,
    price numeric(10,2) NOT NULL CHECK (price > 0)
);
COMMENT ON TABLE shop.products IS 'Каталог товаров';

CREATE TABLE shop.orders (
    id integer PRIMARY KEY,
    customer_id integer NOT NULL REFERENCES shop.customers (id),
    status text NOT NULL CHECK (status IN ('new', 'paid', 'shipped', 'cancelled')),
    created_at timestamp with time zone NOT NULL
);
COMMENT ON TABLE shop.orders IS 'Заказы';
COMMENT ON COLUMN shop.orders.status IS 'new, paid, shipped или cancelled';
CREATE INDEX orders_created_idx ON shop.orders (created_at);
CREATE INDEX orders_customer_idx ON shop.orders (customer_id);

CREATE TABLE shop.order_items (
    order_id integer NOT NULL REFERENCES shop.orders (id),
    product_id integer NOT NULL REFERENCES shop.products (id),
    quantity integer NOT NULL CHECK (quantity > 0),
    price numeric(10,2) NOT NULL,
    PRIMARY KEY (order_id, product_id)
);
COMMENT ON TABLE shop.order_items IS 'Позиции заказов: цена фиксируется на момент заказа';

CREATE TABLE hr.employees (
    id integer PRIMARY KEY,
    full_name text NOT NULL,
    salary numeric(12,2) NOT NULL
);
COMMENT ON TABLE hr.employees IS 'Сотрудники и зарплаты: агенту доступа нет';

INSERT INTO shop.customers (id, full_name, email, city, created_at)
SELECT g,
       'Customer ' || g,
       'customer' || g || '@example.com',
       (ARRAY['Москва', 'Санкт-Петербург', 'Казань', 'Новосибирск', NULL])[1 + g % 5],
       timestamptz '2025-01-01 09:00:00+00' + (g * interval '5 days')
FROM generate_series(1, 50) AS g;

INSERT INTO shop.products (id, name, category, price)
SELECT g,
       'Product ' || g,
       (ARRAY['books', 'toys', 'tools', 'garden'])[1 + g % 4],
       10 + (g * 7) % 90 + 0.99
FROM generate_series(1, 20) AS g;

-- (g * 137) % 500 runs through every number below 500 once, so the status ranges give exact counts
-- and the first orders already show different statuses.
INSERT INTO shop.orders (id, customer_id, status, created_at)
SELECT g,
       1 + (g * 7) % 50,
       CASE WHEN (g * 137) % 500 < 174 THEN 'shipped'
            WHEN (g * 137) % 500 < 336 THEN 'paid'
            WHEN (g * 137) % 500 < 426 THEN 'cancelled'
            ELSE 'new' END,
       timestamptz '2026-01-01 00:00:00+00' + (g * interval '7 hours')
FROM generate_series(1, 500) AS g;

-- Four items for three orders in five and five for the other two: 2200 rows in all.
INSERT INTO shop.order_items (order_id, product_id, quantity, price)
SELECT o.id,
       1 + (o.id + k * 3) % 20,
       1 + (o.id + k) % 3,
       p.price
FROM shop.orders AS o
JOIN LATERAL generate_series(1, 4 + CASE WHEN o.id % 5 IN (0, 1) THEN 1 ELSE 0 END) AS k ON true
JOIN shop.products AS p ON p.id = 1 + (o.id + k * 3) % 20;

INSERT INTO hr.employees (id, full_name, salary) VALUES
    (1, 'Анна Смирнова', 250000),
    (2, 'Пётр Иванов', 180000);

ANALYZE shop.customers;
ANALYZE shop.products;
ANALYZE shop.orders;
ANALYZE shop.order_items;
ANALYZE hr.employees;
