import os
from datetime import datetime, timezone
from functools import lru_cache

from fastapi import HTTPException
from redis import Redis
from redis.exceptions import RedisError

MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE_SIZE", "50000"))
QUEUE_DEMAND_THRESHOLD = int(os.getenv("QUEUE_DEMAND_THRESHOLD", "500"))
DEFAULT_CONCERT_ID = os.getenv("DEFAULT_CONCERT_ID", "1")
QUEUE_ADMISSION_TTL_SECONDS = int(os.getenv("QUEUE_ADMISSION_TTL_SECONDS", "300"))

JOIN_QUEUE_SCRIPT = """
local queue_key = KEYS[1]
local seq_key = KEYS[2]
local user_id = ARGV[1]
local max_size = tonumber(ARGV[2])

local existing_rank = redis.call('ZRANK', queue_key, user_id)
if existing_rank then
  return {1, existing_rank + 1, redis.call('ZCARD', queue_key)}
end

local current_size = redis.call('ZCARD', queue_key)
if current_size >= max_size then
  return {0, -1, current_size}
end

local seq = redis.call('INCR', seq_key)
redis.call('ZADD', queue_key, seq, user_id)
return {2, current_size + 1, current_size + 1}
"""


@lru_cache
def get_redis_client() -> Redis:
    redis_password = os.getenv("REDIS_PASSWORD") or None
    return Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        db=int(os.getenv("REDIS_DB", "0")),
        password=redis_password,
        decode_responses=True,
        socket_connect_timeout=float(os.getenv("REDIS_CONNECT_TIMEOUT", "2")),
        socket_timeout=float(os.getenv("REDIS_SOCKET_TIMEOUT", "2")),
    )


def check_redis_connection() -> bool:
    try:
        get_redis_client().ping()
        return True
    except RedisError:
        return False


def queue_key(concert_id: int | str) -> str:
    return f"queue:concert:{concert_id}:zset"


def queue_seq_key(concert_id: int | str) -> str:
    return f"queue:concert:{concert_id}:seq"


def admission_key(concert_id: int | str, user_id: str) -> str:
    return f"queue:admitted:{concert_id}:{user_id}"


def demand_key(concert_id: int | str) -> str:
    return f"queue:concert:{concert_id}:demand"


def default_queue_key() -> str:
    return queue_key(DEFAULT_CONCERT_ID)


def _normalize_perform_window(perform: dict, now: datetime) -> tuple[datetime, datetime, datetime]:
    opens_at = perform["booking_opens_at"]
    closes_at = perform["booking_closes_at"]
    if opens_at.tzinfo is None:
        opens_at = opens_at.replace(tzinfo=timezone.utc)
    if closes_at.tzinfo is None:
        closes_at = closes_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now, opens_at, closes_at


def is_booking_window_open(perform: dict, now: datetime | None = None) -> bool:
    if perform["status"] != "OPEN":
        return False

    now = now or datetime.now(timezone.utc)
    now, opens_at, closes_at = _normalize_perform_window(perform, now)
    return opens_at <= now <= closes_at


def register_booking_demand(concert_id: int | str, user_id: str) -> int:
    """예매 시도 사용자를 등록하고 현재 수요(고유 사용자 수)를 반환한다."""
    client = get_redis_client()
    key = demand_key(concert_id)
    try:
        client.sadd(key, str(user_id))
        return int(client.scard(key))
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Failed to update booking demand") from exc


def get_booking_demand_count(concert_id: int | str) -> int:
    try:
        return int(get_redis_client().scard(demand_key(concert_id)))
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Failed to read booking demand") from exc


def requires_queue_admission(
    concert_id: int | str,
    user_id: str,
    perform: dict,
    now: datetime | None = None,
) -> bool:
    """오픈 시간 이후 예매 시도 사용자가 임계값(기본 500)을 초과하면 대기열 입장 권한이 필요하다."""
    if not is_booking_window_open(perform, now):
        return False

    demand = register_booking_demand(concert_id, user_id)
    return demand > QUEUE_DEMAND_THRESHOLD


def is_queue_required(perform: dict, now: datetime | None = None) -> bool:
    """레거시 호환: 오픈 시간 중이며 현재 수요가 임계값을 초과했는지 조회한다."""
    if not is_booking_window_open(perform, now):
        return False
    return get_booking_demand_count(perform["perform_id"]) > QUEUE_DEMAND_THRESHOLD


def ensure_booking_window(perform: dict, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    if perform["status"] != "OPEN":
        raise HTTPException(status_code=400, detail="예매가 불가능한 공연입니다.")

    now, opens_at, closes_at = _normalize_perform_window(perform, now)
    if not (opens_at <= now <= closes_at):
        raise HTTPException(status_code=400, detail="예매 가능 시간이 아닙니다.")


def join_waiting_queue(concert_id: int | str, user_id: str) -> tuple[int, int, bool]:
    """Returns queue_number, queue_length, is_new_join."""
    client = get_redis_client()
    key = queue_key(concert_id)
    seq_key_name = queue_seq_key(concert_id)

    try:
        result = client.eval(
            JOIN_QUEUE_SCRIPT,
            2,
            key,
            seq_key_name,
            user_id,
            MAX_QUEUE_SIZE,
        )
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Failed to update Redis queue") from exc

    result_code = int(result[0])
    queue_number = int(result[1])
    queue_length = int(result[2])

    if result_code == 0:
        raise HTTPException(
            status_code=429,
            detail={
                "message": "대기열이 가득 찼습니다.",
                "queueLength": queue_length,
                "maxQueueSize": MAX_QUEUE_SIZE,
            },
        )

    return queue_number, queue_length, result_code == 2


def get_user_queue_status(concert_id: int | str, user_id: str) -> tuple[int | None, int]:
    client = get_redis_client()
    key = queue_key(concert_id)

    try:
        rank = client.zrank(key, user_id)
        queue_length = client.zcard(key)
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Failed to read Redis queue") from exc

    position = rank + 1 if rank is not None else None
    return position, queue_length


def get_concert_queue_length(concert_id: int | str) -> int:
    client = get_redis_client()
    try:
        return client.zcard(queue_key(concert_id))
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Failed to read Redis queue") from exc


def process_waiting_queue(concert_id: int | str, count: int) -> tuple[list[tuple[str, float]], int]:
    client = get_redis_client()
    key = queue_key(concert_id)

    try:
        popped = client.zpopmin(key, count)
        queue_length = client.zcard(key)
        for user_id, _score in popped:
            client.setex(
                admission_key(concert_id, str(user_id)),
                QUEUE_ADMISSION_TTL_SECONDS,
                "1",
            )
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Failed to consume Redis queue") from exc

    return popped, queue_length


def grant_queue_admission(concert_id: int | str, user_id: str) -> None:
    try:
        get_redis_client().setex(
            admission_key(concert_id, str(user_id)),
            QUEUE_ADMISSION_TTL_SECONDS,
            "1",
        )
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Failed to grant queue admission") from exc


def has_queue_admission(concert_id: int | str, user_id: str) -> bool:
    try:
        return bool(get_redis_client().exists(admission_key(concert_id, user_id)))
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Failed to read queue admission") from exc


def require_queue_admission(concert_id: int | str, user_id: str) -> None:
    if not has_queue_admission(concert_id, user_id):
        raise HTTPException(
            status_code=403,
            detail="예매 권한이 없습니다. 대기열에 참여한 뒤 순번이 도래할 때까지 기다려주세요.",
        )


def consume_queue_admission(concert_id: int | str, user_id: str) -> None:
    try:
        get_redis_client().delete(admission_key(concert_id, user_id))
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Failed to consume queue admission") from exc
