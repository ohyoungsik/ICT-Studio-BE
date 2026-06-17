import os
from functools import lru_cache
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field
from redis import Redis
from redis.exceptions import RedisError

app = FastAPI(
    title="ICT Studio API",
    description="ICT Studio 공연 예매 서비스 API 명세 (더미 데이터 기반)",
    version="0.1.0",
    openapi_tags=[
        {"name": "인증", "description": "회원가입, 로그인, 로그아웃"},
        {"name": "공연", "description": "공연 목록 및 상세 조회"},
        {"name": "좌석", "description": "공연별 좌석 조회"},
        {"name": "예매", "description": "예매 생성 및 조회 (인증 필요)"},
    ],
)

cors_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer(auto_error=False)

# --- 더미 데이터 ---

DUMMY_CONCERTS = [
    {
        "id": "ict-concert-2026",
        "title": "ICT Studio 2026 콘서트",
        "artist": "ICT Orchestra",
        "venue": "ICT 아레나",
        "date": "2026-06-15T19:00:00+09:00",
        "status": "OPEN",
        "description": "ICT Studio의 첫 번째 공연입니다.",
        "price": 50000,
    },
    {
        "id": "ict-concert-2025",
        "title": "ICT Studio 2025 콘서트",
        "artist": "ICT Orchestra",
        "venue": "ICT 아레나",
        "date": "2025-12-20T19:00:00+09:00",
        "status": "CLOSED",
        "description": "지난 시즌 공연입니다.",
        "price": 45000,
    },
]

DUMMY_SEATS: dict[str, list[dict]] = {
    "ict-concert-2026": [
        {"id": "A1", "row": "A", "number": 1, "status": "AVAILABLE", "price": 50000},
        {"id": "A2", "row": "A", "number": 2, "status": "AVAILABLE", "price": 50000},
        {"id": "A3", "row": "A", "number": 3, "status": "AVAILABLE", "price": 50000},
        {"id": "A4", "row": "A", "number": 4, "status": "BOOKED", "price": 50000},
        {"id": "B1", "row": "B", "number": 1, "status": "AVAILABLE", "price": 45000},
        {"id": "B2", "row": "B", "number": 2, "status": "AVAILABLE", "price": 45000},
        {"id": "B3", "row": "B", "number": 3, "status": "BOOKED", "price": 45000},
    ],
    "ict-concert-2025": [
        {"id": "A1", "row": "A", "number": 1, "status": "BOOKED", "price": 45000},
        {"id": "A2", "row": "A", "number": 2, "status": "BOOKED", "price": 45000},
    ],
}

# 인메모리 저장소 (DB 대체)
users: dict[str, dict] = {}
sessions: dict[str, str] = {}  # token -> user_id
bookings: dict[str, dict] = {}
booking_counter = 0


# --- Request / Response 모델 ---

class SignupRequest(BaseModel):
    name: str = Field(..., min_length=1, examples=["홍길동"])
    email: EmailStr = Field(..., examples=["user@example.com"])
    password: str = Field(..., min_length=6, examples=["password123"])


class LoginRequest(BaseModel):
    email: EmailStr = Field(..., examples=["user@example.com"])
    password: str = Field(..., examples=["password123"])


class UserResponse(BaseModel):
    id: str = Field(..., examples=["user-001"])
    name: str = Field(..., examples=["홍길동"])
    email: str = Field(..., examples=["user@example.com"])


class LoginResponse(BaseModel):
    token: str = Field(..., examples=["token-abc123"])
    user: UserResponse


class MessageResponse(BaseModel):
    message: str = Field(..., examples=["로그아웃되었습니다."])


class ConcertResponse(BaseModel):
    id: str = Field(..., examples=["ict-concert-2026"])
    title: str
    artist: str
    venue: str
    date: str
    status: Literal["OPEN", "CLOSED"]
    description: str
    price: int


class ConcertListResponse(BaseModel):
    concerts: list[ConcertResponse]
    page: int = Field(..., examples=[1])
    size: int = Field(..., examples=[10])
    total: int = Field(..., examples=[2])


class SeatResponse(BaseModel):
    id: str = Field(..., examples=["A2"])
    row: str
    number: int
    status: Literal["AVAILABLE", "BOOKED"]
    price: int


class SeatListResponse(BaseModel):
    concertId: str = Field(..., examples=["ict-concert-2026"])
    seats: list[SeatResponse]


class CreateBookingRequest(BaseModel):
    concertId: str = Field(..., examples=["ict-concert-2026"])
    seatIds: list[str] = Field(..., min_length=1, examples=[["A2", "A3"]])


class BookingResponse(BaseModel):
    id: str = Field(..., examples=["booking-001"])
    userId: str
    concertId: str
    concertTitle: str
    seatIds: list[str]
    totalPrice: int
    status: Literal["CONFIRMED"]
    createdAt: str


class BookingListResponse(BaseModel):
    bookings: list[BookingResponse]


class ErrorResponse(BaseModel):
    detail: str


class QueueJoinRequest(BaseModel):
    concertId: int | str = Field(1, examples=[1])
    userId: str = Field(..., min_length=1, examples=["user-1"])


class QueueJoinResponse(BaseModel):
    status: Literal["WAITING"]
    queueNumber: int = Field(..., examples=[152])
    message: str


class QueueStatusResponse(BaseModel):
    concertId: int | str = Field(..., examples=[1])
    userId: str = Field(..., examples=["user-1"])
    status: Literal["WAITING", "NOT_FOUND"]
    position: int | None = Field(None, examples=[32])
    queueLength: int


class QueueLengthResponse(BaseModel):
    concertId: int | str = Field(..., examples=[1])
    queueLength: int


class DefaultQueueLengthResponse(BaseModel):
    queueLength: int


class QueueProcessRequest(BaseModel):
    concertId: int | str = Field(1, examples=[1])
    count: int = Field(1, ge=1, le=1000, examples=[100])


class ProcessedQueueItem(BaseModel):
    userId: str = Field(..., examples=["user-1"])
    score: float = Field(..., examples=[152])


class QueueProcessResponse(BaseModel):
    status: Literal["PROCESSED", "EMPTY"]
    concertId: int | str = Field(..., examples=[1])
    processedCount: int = Field(..., examples=[100])
    queueLength: int = Field(..., examples=[9900])
    users: list[ProcessedQueueItem]
    message: str


class CreateReservationRequest(BaseModel):
    concertId: int | str = Field(..., examples=[1])
    userId: str = Field(..., min_length=1, examples=["user-1"])
    seatId: str = Field(..., min_length=1, examples=["A-10"])


class ReservationCreatedResponse(BaseModel):
    status: Literal["RESERVED"]
    reservationId: int


reservations: dict[int, dict] = {}
reservation_counter = 1000
MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE_SIZE", "10000"))
DEFAULT_CONCERT_ID = os.getenv("DEFAULT_CONCERT_ID", "1")

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


def _queue_key(concert_id: int | str) -> str:
    return f"queue:concert:{concert_id}:zset"


def _queue_seq_key(concert_id: int | str) -> str:
    return f"queue:concert:{concert_id}:seq"


def _default_queue_key() -> str:
    return _queue_key(DEFAULT_CONCERT_ID)


@app.get("/api/health/redis", tags=["시스템"], summary="Redis 헬스체크")
def redis_health_check():
    try:
        get_redis_client().ping()
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Redis queue is unavailable") from exc
    return {"status": "ok"}


# --- 인증 헬퍼 ---

def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)] = None,
) -> dict:
    if not credentials:
        raise HTTPException(status_code=401, detail="인증 토큰이 필요합니다.")

    user_id = sessions.get(credentials.credentials)
    if not user_id or user_id not in users:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다.")

    return users[user_id]


def _find_concert(concert_id: str) -> dict:
    for concert in DUMMY_CONCERTS:
        if concert["id"] == concert_id:
            return concert
    raise HTTPException(status_code=404, detail="공연을 찾을 수 없습니다.")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@app.get("/health", tags=["시스템"], summary="ALB 헬스체크")
@app.get("/api/health", tags=["시스템"], summary="애플리케이션 헬스체크")
def health_check():
    return {"status": "ok"}


# --- Queue / load-test API ---

@app.post(
    "/api/queue/join",
    response_model=QueueJoinResponse,
    tags=["Queue"],
    summary="Join Redis-backed waiting queue",
    responses={
        429: {"description": "Queue is full"},
        503: {"model": ErrorResponse, "description": "Redis unavailable"},
    },
)
def join_queue(body: QueueJoinRequest, response: Response):
    redis_client = get_redis_client()
    key = _queue_key(body.concertId)
    seq_key = _queue_seq_key(body.concertId)

    try:
        result = redis_client.eval(
            JOIN_QUEUE_SCRIPT,
            2,
            key,
            seq_key,
            body.userId,
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
                "message": "Queue is full.",
                "queueLength": queue_length,
                "maxQueueSize": MAX_QUEUE_SIZE,
            },
        )

    if result_code == 2:
        response.status_code = 202

    return QueueJoinResponse(
        status="WAITING",
        queueNumber=queue_number,
        message="Queue registration completed.",
    )


@app.get(
    "/api/queue/status/{concert_id}/{user_id}",
    response_model=QueueStatusResponse,
    tags=["Queue"],
    summary="Get queue status for a user",
    responses={503: {"model": ErrorResponse, "description": "Redis unavailable"}},
)
def get_queue_status(concert_id: str, user_id: str):
    redis_client = get_redis_client()
    key = _queue_key(concert_id)

    try:
        rank = redis_client.zrank(key, user_id)
        queue_length = redis_client.zcard(key)
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Failed to read Redis queue") from exc

    position = rank + 1 if rank is not None else None
    return QueueStatusResponse(
        concertId=concert_id,
        userId=user_id,
        status="WAITING" if position else "NOT_FOUND",
        position=position,
        queueLength=queue_length,
    )


@app.get(
    "/api/queue/length",
    response_model=DefaultQueueLengthResponse,
    tags=["Queue"],
    summary="Get default queue length",
    responses={503: {"model": ErrorResponse, "description": "Redis unavailable"}},
)
def get_default_queue_length():
    redis_client = get_redis_client()

    try:
        length = redis_client.zcard(_default_queue_key())
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Failed to read Redis queue") from exc

    return DefaultQueueLengthResponse(queueLength=length)


@app.get(
    "/api/queue/length/{concert_id}",
    response_model=QueueLengthResponse,
    tags=["Queue"],
    summary="Get queue length",
    responses={503: {"model": ErrorResponse, "description": "Redis unavailable"}},
)
def get_queue_length(concert_id: str):
    redis_client = get_redis_client()
    key = _queue_key(concert_id)

    try:
        length = redis_client.zcard(key)
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Failed to read Redis queue") from exc

    return QueueLengthResponse(concertId=concert_id, queueLength=length)


@app.post(
    "/api/queue/process",
    response_model=QueueProcessResponse,
    tags=["Queue"],
    summary="Consume users from Redis-backed waiting queue",
    responses={503: {"model": ErrorResponse, "description": "Redis unavailable"}},
)
@app.post(
    "/api/queue/worker",
    response_model=QueueProcessResponse,
    tags=["Queue"],
    summary="Worker-compatible queue consume API",
    responses={503: {"model": ErrorResponse, "description": "Redis unavailable"}},
)
def process_queue(body: QueueProcessRequest):
    redis_client = get_redis_client()
    key = _queue_key(body.concertId)

    try:
        popped = redis_client.zpopmin(key, body.count)
        queue_length = redis_client.zcard(key)
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Failed to consume Redis queue") from exc

    users = [
        ProcessedQueueItem(userId=str(user_id), score=float(score))
        for user_id, score in popped
    ]
    processed_count = len(users)

    return QueueProcessResponse(
        status="PROCESSED" if processed_count else "EMPTY",
        concertId=body.concertId,
        processedCount=processed_count,
        queueLength=queue_length,
        users=users,
        message=(
            "Queue users processed."
            if processed_count
            else "Queue is empty."
        ),
    )


@app.post(
    "/api/reservations",
    response_model=ReservationCreatedResponse,
    tags=["Reservations"],
    summary="Create a lightweight reservation for load testing",
)
def create_reservation(body: CreateReservationRequest):
    global reservation_counter

    reservation_counter += 1
    reservations[reservation_counter] = {
        "reservationId": reservation_counter,
        "concertId": body.concertId,
        "userId": body.userId,
        "seatId": body.seatId,
        "createdAt": _utc_now_iso(),
    }

    return ReservationCreatedResponse(status="RESERVED", reservationId=reservation_counter)


# --- 인증 API ---

@app.post(
    "/api/auth/signup",
    response_model=UserResponse,
    tags=["인증"],
    summary="회원가입",
    responses={
        409: {"model": ErrorResponse, "description": "이미 등록된 이메일"},
    },
)
def signup(body: SignupRequest):
    """FE에서 비밀번호 일치 검증 후 `name`, `email`, `password`만 전송합니다."""
    if any(u["email"] == body.email for u in users.values()):
        raise HTTPException(status_code=409, detail="이미 등록된 이메일입니다.")

    user_id = f"user-{uuid4().hex[:8]}"
    users[user_id] = {
        "id": user_id,
        "name": body.name,
        "email": body.email,
        "password": body.password,
    }
    return UserResponse(id=user_id, name=body.name, email=body.email)


@app.post(
    "/api/auth/login",
    response_model=LoginResponse,
    tags=["인증"],
    summary="로그인",
    responses={
        401: {"model": ErrorResponse, "description": "이메일 또는 비밀번호 오류"},
    },
)
def login(body: LoginRequest):
    """로그인 성공 시 `token`을 반환합니다. 이후 API 호출 시 `Authorization: Bearer {token}` 헤더를 사용하세요."""
    user = next(
        (u for u in users.values() if u["email"] == body.email and u["password"] == body.password),
        None,
    )
    if not user:
        raise HTTPException(status_code=401, detail="이메일 또는 비밀번호가 올바르지 않습니다.")

    token = f"token-{uuid4().hex}"
    sessions[token] = user["id"]
    return LoginResponse(
        token=token,
        user=UserResponse(id=user["id"], name=user["name"], email=user["email"]),
    )


@app.post(
    "/api/auth/logout",
    response_model=MessageResponse,
    tags=["인증"],
    summary="로그아웃",
    responses={
        401: {"model": ErrorResponse, "description": "인증 필요"},
    },
)
def logout(
    current_user: Annotated[dict, Depends(get_current_user)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)] = None,
):
    if credentials:
        sessions.pop(credentials.credentials, None)
    return MessageResponse(message="로그아웃되었습니다.")


# --- 공연 API ---

@app.get(
    "/api/concerts",
    response_model=ConcertListResponse,
    tags=["공연"],
    summary="공연 목록 조회",
)
def list_concerts(
    status: str | None = Query(None, description="공연 상태 필터 (예: OPEN, CLOSED)", examples=["OPEN"]),
    page: int = Query(1, ge=1, description="페이지 번호"),
    size: int = Query(10, ge=1, le=100, description="페이지 크기"),
):
    result = DUMMY_CONCERTS
    if status:
        result = [c for c in result if c["status"] == status.upper()]

    start = (page - 1) * size
    end = start + size
    paginated = result[start:end]

    return ConcertListResponse(
        concerts=[ConcertResponse(**c) for c in paginated],
        page=page,
        size=size,
        total=len(result),
    )


@app.get(
    "/api/concerts/{concert_id}",
    response_model=ConcertResponse,
    tags=["공연"],
    summary="공연 상세 조회",
    responses={
        404: {"model": ErrorResponse, "description": "공연 없음"},
    },
)
def get_concert(
    concert_id: Annotated[str, Path(description="공연 ID", examples=["ict-concert-2026"])],
):
    return ConcertResponse(**_find_concert(concert_id))


# --- 좌석 API ---

@app.get(
    "/api/concerts/{concert_id}/seats",
    response_model=SeatListResponse,
    tags=["좌석"],
    summary="좌석 목록 조회",
    responses={
        404: {"model": ErrorResponse, "description": "공연 또는 좌석 정보 없음"},
    },
)
def list_seats(
    concert_id: Annotated[str, Path(description="공연 ID", examples=["ict-concert-2026"])],
):
    _find_concert(concert_id)
    seats = DUMMY_SEATS.get(concert_id)
    if seats is None:
        raise HTTPException(status_code=404, detail="좌석 정보를 찾을 수 없습니다.")

    return SeatListResponse(
        concertId=concert_id,
        seats=[SeatResponse(**s) for s in seats],
    )


# --- 예매 API ---

@app.post(
    "/api/bookings",
    response_model=BookingResponse,
    tags=["예매"],
    summary="예매 생성",
    responses={
        400: {"model": ErrorResponse, "description": "예매 불가 공연"},
        401: {"model": ErrorResponse, "description": "인증 필요"},
        404: {"model": ErrorResponse, "description": "좌석 없음"},
        409: {"model": ErrorResponse, "description": "이미 예매된 좌석"},
    },
)
def create_booking(
    body: CreateBookingRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """`concertId`와 `seatIds`를 전송하여 예매를 생성합니다."""
    global booking_counter

    concert = _find_concert(body.concertId)
    if concert["status"] != "OPEN":
        raise HTTPException(status_code=400, detail="예매가 불가능한 공연입니다.")

    seats = DUMMY_SEATS.get(body.concertId, [])
    seat_map = {s["id"]: s for s in seats}

    for seat_id in body.seatIds:
        seat = seat_map.get(seat_id)
        if not seat:
            raise HTTPException(status_code=404, detail=f"좌석 '{seat_id}'을(를) 찾을 수 없습니다.")
        if seat["status"] != "AVAILABLE":
            raise HTTPException(status_code=409, detail=f"좌석 '{seat_id}'은(는) 이미 예매되었습니다.")

    for seat_id in body.seatIds:
        seat_map[seat_id]["status"] = "BOOKED"

    booking_counter += 1
    booking_id = f"booking-{booking_counter:03d}"
    total_price = sum(seat_map[sid]["price"] for sid in body.seatIds)

    booking = {
        "id": booking_id,
        "userId": current_user["id"],
        "concertId": body.concertId,
        "concertTitle": concert["title"],
        "seatIds": body.seatIds,
        "totalPrice": total_price,
        "status": "CONFIRMED",
        "createdAt": _utc_now_iso(),
    }
    bookings[booking_id] = booking

    return BookingResponse(**booking)


@app.get(
    "/api/bookings/me",
    response_model=BookingListResponse,
    tags=["예매"],
    summary="내 예매 내역 조회",
    responses={
        401: {"model": ErrorResponse, "description": "인증 필요"},
    },
)
def list_my_bookings(current_user: Annotated[dict, Depends(get_current_user)]):
    my_bookings = [b for b in bookings.values() if b["userId"] == current_user["id"]]
    my_bookings.sort(key=lambda b: b["createdAt"], reverse=True)
    return BookingListResponse(bookings=[BookingResponse(**b) for b in my_bookings])


@app.get(
    "/api/bookings/{booking_id}",
    response_model=BookingResponse,
    tags=["예매"],
    summary="예매 상세 조회",
    responses={
        401: {"model": ErrorResponse, "description": "인증 필요"},
        403: {"model": ErrorResponse, "description": "접근 권한 없음"},
        404: {"model": ErrorResponse, "description": "예매 없음"},
    },
)
def get_booking(
    booking_id: Annotated[str, Path(description="예매 ID", examples=["booking-001"])],
    current_user: Annotated[dict, Depends(get_current_user)],
):
    booking = bookings.get(booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="예매 내역을 찾을 수 없습니다.")
    if booking["userId"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")

    return BookingResponse(**booking)
