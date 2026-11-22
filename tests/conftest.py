import datetime
import sqlite3

import pytest
import pytest_asyncio
from datasette.app import Datasette

FUELS = ["Solar", "Hydro", "Wind", "Gas", "Coal", "Oil"]
STATUSES = ["paid", "paid", "paid", "pending", "refunded"]


def build_database(path):
    """
    A small database covering the shapes the profiler has to recognise.
    Everything is generated from the row number, so it is deterministic.
    """
    conn = sqlite3.connect(path)
    conn.executescript("""
        create table customers (id integer primary key, name text);
        create table orders (
            id integer primary key,
            created_at text,
            status text,
            price real,
            quantity integer,
            customer_id integer references customers(id),
            note text,
            shipped_at integer,
            is_gift integer,
            "value" real
        );
        create table places (
            id integer primary key,
            name text,
            latitude real,
            longitude real,
            fuel text,
            capacity_mw real,
            elevation integer,
            built_year integer
        );
        create table readings (
            sensor text,
            day text,
            reading real,
            primary key (sensor, day)
        );
        create table codes (code text primary key, label text) without rowid;
        create view paid_orders as
            select id, created_at, price, quantity from orders where status = 'paid';
        """)
    conn.executemany(
        "insert into customers values (?, ?)",
        [(i, f"Customer {i}") for i in range(1, 6)],
    )
    start = datetime.datetime(2023, 1, 1, 8, 0, 0)
    orders = []
    for i in range(600):
        moment = start + datetime.timedelta(hours=37 * i)
        orders.append(
            (
                i + 1,
                moment.strftime("%Y-%m-%d %H:%M:%S"),
                STATUSES[i % len(STATUSES)] if i % 50 else None,
                round(5 + (i * 7919 % 1000) / 10, 2),
                1 + i % 4,
                1 + (i * 3) % 5,
                f"Note for order {i} with some free text",
                1700000000 + i * 3600,
                i % 2,
                float(i % 9),
            )
        )
    conn.executemany("insert into orders values (?,?,?,?,?,?,?,?,?,?)", orders)
    places = []
    for i in range(300):
        places.append(
            (
                i + 1,
                f"Plant {i}",
                -60 + (i * 37 % 120) + 0.25,
                -170 + (i * 53 % 340) + 0.5,
                FUELS[i % len(FUELS)],
                # Long-tailed: most are small, a few are huge
                float((i % 25 + 1) ** 6),
                1000 + (i * 7) % 1200,
                1950 + i % 70,
            )
        )
    conn.executemany("insert into places values (?,?,?,?,?,?,?,?)", places)
    readings = []
    day = datetime.date(2025, 1, 1)
    for d in range(120):
        for sensor in ("north", "south", "east"):
            readings.append(
                (
                    sensor,
                    (day + datetime.timedelta(days=d)).isoformat(),
                    d + len(sensor),
                )
            )
    conn.executemany("insert into readings values (?,?,?)", readings)
    conn.executemany(
        "insert into codes values (?, ?)",
        [(f"c{i}", f"Label {i % 3}") for i in range(40)],
    )
    conn.commit()
    conn.close()


@pytest.fixture(scope="session")
def db_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("dbs") / "shop.db"
    build_database(path)
    return str(path)


@pytest_asyncio.fixture
async def ds(db_path):
    datasette = Datasette([db_path])
    await datasette.invoke_startup()
    return datasette
