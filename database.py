import os
import re
import time
from contextlib import contextmanager
from threading import Lock
from typing import Any, Generator

import bcrypt
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DEFAULT_SEAT_PRICE = 50_000
SEED_USER_COUNT = int(os.getenv("SEED_USER_COUNT", "1100"))
SEED_BOOKING_OPEN_DELAY_MINUTES = int(os.getenv("SEED_BOOKING_OPEN_DELAY_MINUTES", "10"))
SEED_SEAT_COUNT = int(os.getenv("SEED_SEAT_COUNT", str(SEED_USER_COUNT)))
SEED_USER_PASSWORD = os.getenv("SEED_USER_PASSWORD", "loadtest1234")
SEED_PERFORM_NAME = os.getenv("SEED_PERFORM_NAME", "ICT Studio k6 Load Test Concert")
SEED_MAX_TICKETS_PER_USER = int(os.getenv("SEED_MAX_TICKETS_PER_USER", "1"))

# DB 서버 프로비저닝 직후에는 연결이 잠시 불가할 수 있어 재시도하며 대기한다.
DB_READY_MAX_RETRIES = int(os.getenv("DB_READY_MAX_RETRIES", "60"))
DB_READY_RETRY_INTERVAL = float(os.getenv("DB_READY_RETRY_INTERVAL", "2"))
DB_PROBE_CONNECT_TIMEOUT = int(os.getenv("DB_PROBE_CONNECT_TIMEOUT", "3"))

# 여러 워커/레플리카가 동시에 시드를 시도해도 한 번만 실행되도록 하는 advisory lock 키.
SEED_ADVISORY_LOCK_KEY = int(os.getenv("SEED_ADVISORY_LOCK_KEY", "911002"))

# 쓰기(Primary)용 풀과 읽기(Replica)용 풀을 분리한다.
# 읽기 엔드포인트(DB_READ_HOST / DATABASE_READ_URL)가 없으면 _read_pool은 None이고,
# 이때 get_read_connection()은 쓰기 풀로 fallback 한다(단일 DB 로컬 개발 호환).
_pool: ConnectionPool | None = None
_read_pool: ConnectionPool | None = None
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


def _build_read_conninfo() -> str | None:
    """읽기 전용(Replica) 접속 문자열. 별도 읽기 엔드포인트가 없으면 None."""
    read_url = os.getenv("DATABASE_READ_URL")
    if read_url:
        return read_url

    read_host = os.getenv("DB_READ_HOST")
    if not read_host:
        return None

    read_port = os.getenv("DB_READ_PORT", "5433")
    name = os.getenv("DB_NAME", "ticketing")
    user = os.getenv("DB_USER", "appuser")
    password = os.getenv("DB_PASSWORD", "app_password")
    return f"host={read_host} port={read_port} dbname={name} user={user} password={password}"


def init_pool() -> None:
    global _pool, _read_pool
    with _pool_lock:
        if _pool is None:
            _pool = _create_pool(_build_conninfo())

        if _read_pool is None:
            read_conninfo = _build_read_conninfo()
            if read_conninfo is not None:
                _read_pool = _create_pool(read_conninfo)


def _create_pool(conninfo: str) -> ConnectionPool:
    return ConnectionPool(
        conninfo=conninfo,
        min_size=1,
        max_size=10,
        kwargs={"row_factory": dict_row},
        open=True,
    )


def close_pool() -> None:
    global _pool, _read_pool
    with _pool_lock:
        pool = _pool
        read_pool = _read_pool
        _pool = None
        _read_pool = None

    for p in (pool, read_pool):
        if p is not None:
            try:
                p.close()
            except Exception:
                pass


def reset_pool() -> None:
    global _pool, _read_pool
    with _pool_lock:
        old_pool = _pool
        old_read_pool = _read_pool
        _pool = _create_pool(_build_conninfo())

        read_conninfo = _build_read_conninfo()
        _read_pool = _create_pool(read_conninfo) if read_conninfo is not None else None

    for p in (old_pool, old_read_pool):
        if p is not None:
            try:
                p.close()
            except Exception:
                pass


@contextmanager
def get_connection() -> Generator[Any, None, None]:
    pool = _pool
    if pool is None:
        raise RuntimeError("Database pool is not initialized")

    with pool.connection() as conn:
        yield conn


@contextmanager
def get_read_connection() -> Generator[Any, None, None]:
    """읽기 전용(Replica) 커넥션. 읽기 풀이 없으면 쓰기 풀로 fallback 한다."""
    pool = _read_pool or _pool
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


def check_read_connection() -> bool:
    try:
        with get_read_connection() as conn:
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


# ---------------------------------------------------------------------------
# 스키마 정의 (init.sql 과 동일한 구조)
# ---------------------------------------------------------------------------
# DB 서버가 프로비저닝되면 애플리케이션이 직접 테이블을 보장(IF NOT EXISTS)하므로
# postgres init.sql 적용 여부와 무관하게 항상 동일한 스키마가 준비된다.
_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS perform_info (
        perform_id BIGSERIAL PRIMARY KEY,
        perform_name VARCHAR(200) NOT NULL,
        booking_opens_at TIMESTAMPTZ NOT NULL,
        booking_closes_at TIMESTAMPTZ NOT NULL,
        max_tickets_per_user INTEGER NOT NULL DEFAULT 1,
        status VARCHAR(20) NOT NULL
            CHECK (status IN ('READY', 'OPEN', 'SOLD_OUT', 'CLOSED', 'CANCELLED')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id BIGSERIAL PRIMARY KEY,
        name VARCHAR(100) NOT NULL,
        email VARCHAR(320) NOT NULL UNIQUE,
        password VARCHAR(255) NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS seat_status (
        seat_id BIGSERIAL PRIMARY KEY,
        perform_id BIGINT NOT NULL,
        seat_no VARCHAR(20) NOT NULL,
        status VARCHAR(20) NOT NULL
            CHECK (status IN ('AVAILABLE', 'BOOKED')),
        CONSTRAINT fk_seat_perform
            FOREIGN KEY (perform_id)
            REFERENCES perform_info(perform_id)
            ON DELETE CASCADE,
        CONSTRAINT uq_perform_seat
            UNIQUE (perform_id, seat_no)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS booking (
        booking_id BIGSERIAL PRIMARY KEY,
        perform_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        seat_id BIGINT NOT NULL,
        booked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT fk_booking_perform
            FOREIGN KEY (perform_id) REFERENCES perform_info(perform_id),
        CONSTRAINT fk_booking_user
            FOREIGN KEY (user_id) REFERENCES users(user_id),
        CONSTRAINT fk_booking_seat
            FOREIGN KEY (seat_id) REFERENCES seat_status(seat_id),
        CONSTRAINT uq_booking_seat
            UNIQUE (seat_id)
    )
    """,
)


def _probe_connection() -> bool:
    try:
        with psycopg.connect(
            _build_conninfo(),
            connect_timeout=DB_PROBE_CONNECT_TIMEOUT,
        ) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


def wait_for_db(
    max_retries: int = DB_READY_MAX_RETRIES,
    interval: float = DB_READY_RETRY_INTERVAL,
) -> bool:
    """DB 서버가 응답할 때까지 재시도하며 대기한다."""
    for attempt in range(1, max_retries + 1):
        if _probe_connection():
            print(f"[database] DB connection established (attempt {attempt})", flush=True)
            return True
        print(
            f"[database] DB not ready (attempt {attempt}/{max_retries}); "
            f"retry in {interval}s",
            flush=True,
        )
        time.sleep(interval)
    return False


def init_schema() -> None:
    """핵심 테이블을 생성한다 (이미 존재하면 그대로 둔다)."""
    with get_connection() as conn:
        with conn.transaction():
            # 여러 워커/레플리카가 동시에 CREATE TABLE 을 실행하면 PostgreSQL 카탈로그
            # 경쟁(pg_type unique violation)이 발생할 수 있어 advisory lock 으로 직렬화한다.
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (SEED_ADVISORY_LOCK_KEY,))
            for statement in _SCHEMA_STATEMENTS:
                conn.execute(statement)
    print("[database] schema ensured", flush=True)


def bootstrap_database() -> None:
    """DB 서버 프로비저닝 이후 연결 대기 → 테이블 생성 → 더미 데이터 시드를 자동 수행한다."""
    if not wait_for_db():
        raise RuntimeError(
            "Database is not reachable after waiting; aborting startup. "
            "DB 서버 프로비저닝 상태와 접속 정보를 확인하세요."
        )
    init_pool()
    init_schema()
    seed_if_empty()


def seed_if_empty() -> None:
    with get_connection() as conn:
        with conn.transaction():
            # 동시에 기동되는 여러 워커/레플리카 중 하나만 시드하도록 직렬화한다.
            # advisory xact lock 은 트랜잭션 종료 시 자동 해제된다.
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (SEED_ADVISORY_LOCK_KEY,))

            count = conn.execute(
                "SELECT COUNT(*) AS count FROM perform_info"
            ).fetchone()["count"]
            if count > 0:
                return

            password_hash = bcrypt.hashpw(
                SEED_USER_PASSWORD.encode(), bcrypt.gensalt()
            ).decode()
            # 콘서트 오픈 시각은 시드 실행(=프로비저닝 직후) 기준 NOW() + 지연(분)으로 잡는다.
            # max_tickets_per_user 는 "1인 1매" 정책을 위해 기본 1로 설정한다.
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
                    %s,
                    'OPEN'
                )
                RETURNING perform_id
                """,
                (
                    SEED_PERFORM_NAME,
                    SEED_BOOKING_OPEN_DELAY_MINUTES,
                    SEED_MAX_TICKETS_PER_USER,
                ),
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
