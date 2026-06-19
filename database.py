import os
import re
from contextlib import contextmanager
from typing import Any, Generator

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DEFAULT_SEAT_PRICE = 50_000

_pool: ConnectionPool | None = None


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
    if _pool is not None:
        return

    _pool = ConnectionPool(
        conninfo=_build_conninfo(),
        min_size=1,
        max_size=10,
        kwargs={"row_factory": dict_row},
        open=True,
    )


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def get_connection() -> Generator[Any, None, None]:
    if _pool is None:
        raise RuntimeError("Database pool is not initialized")

    with _pool.connection() as conn:
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


def seed_if_empty() -> None:
    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) AS count FROM perform_info").fetchone()["count"]
        if count > 0:
            return

        with conn.transaction():
            row = conn.execute(
                """
                INSERT INTO perform_info (
                    perform_name, booking_opens_at, booking_closes_at,
                    max_tickets_per_user, status
                )
                VALUES (%s, NOW() - INTERVAL '1 day', NOW() + INTERVAL '30 days', 4, 'OPEN')
                RETURNING perform_id
                """,
                ("ICT Studio 2026 콘서트",),
            ).fetchone()
            open_perform_id = row["perform_id"]

            conn.execute(
                """
                INSERT INTO perform_info (
                    perform_name, booking_opens_at, booking_closes_at,
                    max_tickets_per_user, status
                )
                VALUES (%s, NOW() - INTERVAL '365 days', NOW() - INTERVAL '1 day', 2, 'CLOSED')
                """,
                ("ICT Studio 2025 콘서트",),
            )

            open_seats = [
                ("A1", "AVAILABLE"),
                ("A2", "AVAILABLE"),
                ("A3", "AVAILABLE"),
                ("A4", "BOOKED"),
                ("B1", "AVAILABLE"),
                ("B2", "AVAILABLE"),
                ("B3", "BOOKED"),
            ]
            for seat_no, status in open_seats:
                conn.execute(
                    """
                    INSERT INTO seat_status (perform_id, seat_no, status)
                    VALUES (%s, %s, %s)
                    """,
                    (open_perform_id, seat_no, status),
                )

            closed_perform_id = conn.execute(
                "SELECT perform_id FROM perform_info WHERE status = 'CLOSED' ORDER BY perform_id LIMIT 1"
            ).fetchone()["perform_id"]
            for seat_no in ("A1", "A2"):
                conn.execute(
                    """
                    INSERT INTO seat_status (perform_id, seat_no, status)
                    VALUES (%s, %s, 'BOOKED')
                    """,
                    (closed_perform_id, seat_no),
                )
