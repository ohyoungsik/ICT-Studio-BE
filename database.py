import os
import re
from contextlib import contextmanager
from threading import Lock
from typing import Any, Generator

import bcrypt
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DEFAULT_SEAT_PRICE = 50_000
SEED_USER_COUNT = int(os.getenv("SEED_USER_COUNT", "1100"))
SEED_BOOKING_OPEN_DELAY_MINUTES = int(os.getenv("SEED_BOOKING_OPEN_DELAY_MINUTES", "10"))
SEED_SEAT_COUNT = int(os.getenv("SEED_SEAT_COUNT", str(SEED_USER_COUNT)))
SEED_USER_PASSWORD = os.getenv("SEED_USER_PASSWORD", "loadtest1234")

_pool: ConnectionPool | None = None
_pool_lock = Lock()


def _build_conninfo() -> str:
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return database_url

    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME", "ticketing")
    user = os.getenv("DB_USER", "appuser")
    password = os.getenv("DB_PASSWORD", "app_password")
    return f"host={host} port={port} dbname={name} user={user} password={password}"


def init_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            return

        _pool = _create_pool()


def _create_pool() -> ConnectionPool:
    return ConnectionPool(
        conninfo=_build_conninfo(),
        min_size=1,
        max_size=10,
        kwargs={"row_factory": dict_row},
        open=True,
    )


def close_pool() -> None:
    global _pool
    with _pool_lock:
        pool = _pool
        _pool = None
    if pool is not None:
        pool.close()


def reset_pool() -> None:
    global _pool
    with _pool_lock:
        old_pool = _pool
        _pool = _create_pool()

    if old_pool is not None:
        try:
            old_pool.close()
        except Exception:
            pass


@contextmanager
def get_connection() -> Generator[Any, None, None]:
    pool = _pool
    if pool is None:
        raise RuntimeError("Database pool is not initialized")

    with pool.connection() as conn:
        yield conn


def check_connection() -> bool:
    try:
        with get_connection() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


def parse_seat_no(seat_no: str) -> tuple[str, int]:
    match = re.fullmatch(r"([A-Za-z]+)(\d+)", seat_no)
    if not match:
        return seat_no, 0
    return match.group(1).upper(), int(match.group(2))


def map_perform_status_to_api(status: str) -> str:
    return "OPEN" if status == "OPEN" else "CLOSED"


def _generate_seat_numbers(count: int) -> list[str]:
    seats: list[str] = []
    row_index = 0
    while len(seats) < count:
        row_label = chr(ord("A") + row_index)
        for seat_num in range(1, 51):
            seats.append(f"{row_label}{seat_num}")
            if len(seats) >= count:
                break
        row_index += 1
    return seats


def seed_if_empty() -> None:
    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) AS count FROM perform_info").fetchone()["count"]
        if count > 0:
            return

        password_hash = bcrypt.hashpw(SEED_USER_PASSWORD.encode(), bcrypt.gensalt()).decode()

        with conn.transaction():
            row = conn.execute(
                """
                INSERT INTO perform_info (
                    perform_name, booking_opens_at, booking_closes_at,
                    max_tickets_per_user, status
                )
                VALUES (
                    %s,
                    NOW() + (%s * INTERVAL '1 minute'),
                    NOW() + INTERVAL '7 days',
                    1,
                    'OPEN'
                )
                RETURNING perform_id
                """,
                ("ICT Studio k6 Load Test Concert", SEED_BOOKING_OPEN_DELAY_MINUTES),
            ).fetchone()
            perform_id = row["perform_id"]

            seat_numbers = _generate_seat_numbers(SEED_SEAT_COUNT)
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO seat_status (perform_id, seat_no, status)
                    VALUES (%s, %s, 'AVAILABLE')
                    """,
                    [(perform_id, seat_no) for seat_no in seat_numbers],
                )

            user_rows = [
                (
                    f"Load Test User {user_id}",
                    f"loadtest-user-{user_id}@example.com",
                    password_hash,
                )
                for user_id in range(1, SEED_USER_COUNT + 1)
            ]
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO users (name, email, password)
                    VALUES (%s, %s, %s)
                    """,
                    user_rows,
                )
