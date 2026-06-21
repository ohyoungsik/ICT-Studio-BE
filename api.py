import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import uuid4

import bcrypt
from fastapi import Depends, FastAPI, HTTPException, Path, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg import OperationalError
from pydantic import BaseModel, EmailStr, Field

from database import (
    DEFAULT_SEAT_PRICE,
    check_connection,
    close_pool,
    get_connection,
    init_pool,
    map_perform_status_to_api,
    parse_seat_no,
    reset_pool,
    seed_if_empty,
)
from redis_queue import (
    DEFAULT_CONCERT_ID,
    QUEUE_DEMAND_THRESHOLD,
    check_redis_connection,
    consume_queue_admission,
    ensure_booking_window,
    get_concert_queue_length,
    get_user_queue_status,
    grant_queue_admission,
    has_queue_admission,
    join_waiting_queue,
    process_waiting_queue,
    register_booking_demand,
    require_queue_admission,
    requires_queue_admission,
)

security = HTTPBearer(auto_error=False)

sessions: dict[str, str] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_pool()
    seed_if_empty()
    yield
    close_pool()


app = FastAPI(
    title="ICT Studio API",
    description="ICT Studio 공연 예매 서비스 API (PostgreSQL + Redis 대기열)",
    version="0.3.0",
    lifespan=lifespan,
    openapi_tags=[
        {"name": "인증", "description": "회원가입, 로그인, 로그아웃"},
        {"name": "공연", "description": "공연 목록 및 상세 조회"},
        {"name": "좌석", "description": "공연별 좌석 조회"},
        {"name": "Queue", "description": "Redis 대기열 (예매 가능 시간)"},
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


# --- Request / Response 모델 ---


class SignupRequest(BaseModel):
    name: str = Field(..., min_length=1, examples=["홍길동"])
    email: EmailStr = Field(..., examples=["user@example.com"])
    password: str = Field(..., min_length=6, examples=["password123"])


class LoginRequest(BaseModel):
    email: EmailStr = Field(..., examples=["user@example.com"])
    password: str = Field(..., examples=["password123"])


class UserResponse(BaseModel):
    id: str = Field(..., examples=["1"])
    name: str = Field(..., examples=["홍길동"])
    email: str = Field(..., examples=["user@example.com"])


class LoginResponse(BaseModel):
    token: str = Field(..., examples=["token-abc123"])
    user: UserResponse


class MessageResponse(BaseModel):
    message: str = Field(..., examples=["로그아웃되었습니다."])


class ConcertResponse(BaseModel):
    id: str = Field(..., examples=["1"])
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
    concertId: str = Field(..., examples=["1"])
    seats: list[SeatResponse]


class CreateBookingRequest(BaseModel):
    concertId: str = Field(..., examples=["1"])
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
    concertId: str = Field(..., examples=["1"])
    userId: str | None = Field(None, examples=["1"])


class QueueJoinResponse(BaseModel):
    status: Literal["WAITING", "ADMITTED"]
    queueNumber: int = Field(..., examples=[152])
    message: str


class QueueStatusResponse(BaseModel):
    concertId: str = Field(..., examples=["1"])
    userId: str = Field(..., examples=["1"])
    status: Literal["WAITING", "NOT_FOUND", "ADMITTED"]
    position: int | None = Field(None, examples=[32])
    queueLength: int


class QueueLengthResponse(BaseModel):
    concertId: str = Field(..., examples=["1"])
    queueLength: int


class DefaultQueueLengthResponse(BaseModel):
    queueLength: int


class QueueProcessRequest(BaseModel):
    concertId: str = Field(..., examples=["1"])
    count: int = Field(1, ge=1, le=1000, examples=[100])


class ProcessedQueueItem(BaseModel):
    userId: str = Field(..., examples=["1"])
    score: float = Field(..., examples=[152])


class QueueProcessResponse(BaseModel):
    status: Literal["PROCESSED", "EMPTY"]
    concertId: str = Field(..., examples=["1"])
    processedCount: int = Field(..., examples=[100])
    queueLength: int = Field(..., examples=[9900])
    users: list[ProcessedQueueItem]
    message: str


# --- 헬퍼 ---


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def parse_perform_id(concert_id: str) -> int:
    if not re.fullmatch(r"\d+", concert_id):
        raise HTTPException(status_code=404, detail="공연을 찾을 수 없습니다.")
    return int(concert_id)


def row_to_concert(row: dict) -> ConcertResponse:
    return ConcertResponse(
        id=str(row["perform_id"]),
        title=row["perform_name"],
        artist="ICT Orchestra",
        venue="ICT 아레나",
        date=row["booking_opens_at"].astimezone(timezone.utc).isoformat(),
        status=map_perform_status_to_api(row["status"]),
        description=f"{row['perform_name']} 공연입니다.",
        price=DEFAULT_SEAT_PRICE,
    )


def row_to_seat(row: dict) -> SeatResponse:
    row_label, number = parse_seat_no(row["seat_no"])
    return SeatResponse(
        id=row["seat_no"],
        row=row_label,
        number=number,
        status=row["status"],
        price=DEFAULT_SEAT_PRICE,
    )


def booking_group_key(row: dict) -> tuple:
    booked_at = row["booked_at"]
    if isinstance(booked_at, datetime):
        booked_at = booked_at.replace(microsecond=0)
    return (row["user_id"], row["perform_id"], booked_at)


def rows_to_booking(group_rows: list[dict]) -> BookingResponse:
    first = group_rows[0]
    seat_ids = [row["seat_no"] for row in group_rows]
    return BookingResponse(
        id=f"booking-{first['booking_id']}",
        userId=str(first["user_id"]),
        concertId=str(first["perform_id"]),
        concertTitle=first["perform_name"],
        seatIds=seat_ids,
        totalPrice=DEFAULT_SEAT_PRICE * len(seat_ids),
        status="CONFIRMED",
        createdAt=first["booked_at"].astimezone(timezone.utc).isoformat(),
    )


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)] = None,
) -> dict:
    if not credentials:
        raise HTTPException(status_code=401, detail="인증 토큰이 필요합니다.")

    user_id = sessions.get(credentials.credentials)
    if not user_id:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다.")

    with get_connection() as conn:
        user = conn.execute(
            "SELECT user_id, name, email FROM users WHERE user_id = %s",
            (int(user_id),),
        ).fetchone()

    if not user:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다.")

    return {
        "id": str(user["user_id"]),
        "name": user["name"],
        "email": user["email"],
    }


def fetch_perform(conn, perform_id: int) -> dict:
    row = conn.execute(
        """
        SELECT perform_id, perform_name, booking_opens_at, booking_closes_at,
               max_tickets_per_user, status
        FROM perform_info
        WHERE perform_id = %s
        """,
        (perform_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="공연을 찾을 수 없습니다.")
    return row


def resolve_queue_user_id(
    body: QueueJoinRequest,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)] = None,
) -> str:
    if credentials:
        user_id = sessions.get(credentials.credentials)
        if user_id:
            return user_id
    if body.userId:
        return body.userId
    raise HTTPException(status_code=401, detail="인증 토큰 또는 userId가 필요합니다.")


def run_db_once_with_retry(operation):
    try:
        with get_connection() as conn:
            return operation(conn)
    except OperationalError:
        try:
            reset_pool()
        except Exception as reset_exc:
            raise HTTPException(
                status_code=503,
                detail="Database connection is unavailable",
            ) from reset_exc

        try:
            with get_connection() as conn:
                return operation(conn)
        except OperationalError as retry_exc:
            raise HTTPException(
                status_code=503,
                detail="Database connection is unavailable",
            ) from retry_exc


# --- 헬스체크 ---


@app.get("/health", tags=["시스템"], summary="ALB 헬스체크")
@app.get("/api/health", tags=["시스템"], summary="애플리케이션 헬스체크")
def health_check():
    return {"status": "ok"}


@app.get("/api/health/db", tags=["시스템"], summary="DB 연결 헬스체크")
def health_check_db():
    if not check_connection():
        raise HTTPException(status_code=503, detail="Database connection failed")
    return {"status": "ok", "database": "connected"}


@app.get("/api/health/redis", tags=["시스템"], summary="Redis 헬스체크")
def health_check_redis():
    if not check_redis_connection():
        raise HTTPException(status_code=503, detail="Redis queue is unavailable")
    return {"status": "ok", "redis": "connected"}


# --- Queue API ---


@app.post(
    "/api/queue/join",
    response_model=QueueJoinResponse,
    tags=["Queue"],
    summary="Redis 대기열 입장",
    responses={
        400: {"model": ErrorResponse, "description": "예매 불가 시간"},
        401: {"model": ErrorResponse, "description": "인증 또는 userId 필요"},
        429: {"description": "대기열 만석"},
        503: {"model": ErrorResponse, "description": "Database or Redis unavailable"},
    },
)
def join_queue(
    body: QueueJoinRequest,
    response: Response,
    user_id: Annotated[str, Depends(resolve_queue_user_id)],
):
    perform_id = parse_perform_id(body.concertId)
    perform = run_db_once_with_retry(lambda conn: fetch_perform(conn, perform_id))
    ensure_booking_window(perform)

    demand = register_booking_demand(body.concertId, user_id)
    if demand <= QUEUE_DEMAND_THRESHOLD:
        grant_queue_admission(body.concertId, user_id)
        return QueueJoinResponse(
            status="ADMITTED",
            queueNumber=0,
            message="현재 대기열 없이 바로 예매할 수 있습니다.",
        )

    queue_number, _queue_length, is_new = join_waiting_queue(body.concertId, user_id)
    if is_new:
        response.status_code = 202

    return QueueJoinResponse(
        status="WAITING",
        queueNumber=queue_number,
        message="대기열 등록이 완료되었습니다." if is_new else "이미 대기열에 등록되어 있습니다.",
    )


@app.get(
    "/api/queue/status/{concert_id}/{user_id}",
    response_model=QueueStatusResponse,
    tags=["Queue"],
    summary="대기열 순번 조회",
)
def get_queue_status(
    concert_id: Annotated[str, Path(description="공연 ID")],
    user_id: Annotated[str, Path(description="사용자 ID")],
):
    position, queue_length = get_user_queue_status(concert_id, user_id)
    if has_queue_admission(concert_id, user_id):
        return QueueStatusResponse(
            concertId=concert_id,
            userId=user_id,
            status="ADMITTED",
            position=position,
            queueLength=queue_length,
        )

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
    summary="기본 대기열 길이 조회",
)
def get_default_queue_length():
    return DefaultQueueLengthResponse(
        queueLength=get_concert_queue_length(DEFAULT_CONCERT_ID),
    )


@app.get(
    "/api/queue/length/{concert_id}",
    response_model=QueueLengthResponse,
    tags=["Queue"],
    summary="공연별 대기열 길이 조회",
)
def get_queue_length(concert_id: Annotated[str, Path(description="공연 ID")]):
    return QueueLengthResponse(
        concertId=concert_id,
        queueLength=get_concert_queue_length(concert_id),
    )


@app.post(
    "/api/queue/process",
    response_model=QueueProcessResponse,
    tags=["Queue"],
    summary="대기열 사용자 입장 처리 (워커)",
)
@app.post(
    "/api/queue/worker",
    response_model=QueueProcessResponse,
    tags=["Queue"],
    summary="워커 호환 대기열 소비 API",
)
def process_queue(body: QueueProcessRequest):
    popped, queue_length = process_waiting_queue(body.concertId, body.count)
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
        message="대기열 사용자 입장 처리 완료." if processed_count else "대기열이 비어 있습니다.",
    )


# --- 인증 API ---


@app.post(
    "/api/auth/signup",
    response_model=UserResponse,
    tags=["인증"],
    summary="회원가입",
    responses={409: {"model": ErrorResponse, "description": "이미 등록된 이메일"}},
)
def signup(body: SignupRequest):
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT user_id FROM users WHERE email = %s",
            (body.email,),
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="이미 등록된 이메일입니다.")

        row = conn.execute(
            """
            INSERT INTO users (name, email, password)
            VALUES (%s, %s, %s)
            RETURNING user_id, name, email
            """,
            (body.name, body.email, hash_password(body.password)),
        ).fetchone()

    return UserResponse(id=str(row["user_id"]), name=row["name"], email=row["email"])


@app.post(
    "/api/auth/login",
    response_model=LoginResponse,
    tags=["인증"],
    summary="로그인",
    responses={401: {"model": ErrorResponse, "description": "이메일 또는 비밀번호 오류"}},
)
def login(body: LoginRequest):
    with get_connection() as conn:
        user = conn.execute(
            "SELECT user_id, name, email, password FROM users WHERE email = %s",
            (body.email,),
        ).fetchone()

    if not user or not verify_password(body.password, user["password"]):
        raise HTTPException(status_code=401, detail="이메일 또는 비밀번호가 올바르지 않습니다.")

    token = f"token-{uuid4().hex}"
    sessions[token] = str(user["user_id"])
    return LoginResponse(
        token=token,
        user=UserResponse(
            id=str(user["user_id"]),
            name=user["name"],
            email=user["email"],
        ),
    )


@app.post(
    "/api/auth/logout",
    response_model=MessageResponse,
    tags=["인증"],
    summary="로그아웃",
    responses={401: {"model": ErrorResponse, "description": "인증 필요"}},
)
def logout(
    _: Annotated[dict, Depends(get_current_user)],
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
    params: list = []
    where_clause = ""
    if status:
        db_status = "OPEN" if status.upper() == "OPEN" else None
        if db_status:
            where_clause = "WHERE status = %s"
            params.append(db_status)
        else:
            where_clause = "WHERE status <> 'OPEN'"

    with get_connection() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS count FROM perform_info {where_clause}",
            params,
        ).fetchone()["count"]
        rows = conn.execute(
            f"""
            SELECT perform_id, perform_name, booking_opens_at, booking_closes_at, status
            FROM perform_info
            {where_clause}
            ORDER BY perform_id DESC
            LIMIT %s OFFSET %s
            """,
            [*params, size, (page - 1) * size],
        ).fetchall()

    return ConcertListResponse(
        concerts=[row_to_concert(row) for row in rows],
        page=page,
        size=size,
        total=total,
    )


@app.get(
    "/api/concerts/{concert_id}",
    response_model=ConcertResponse,
    tags=["공연"],
    summary="공연 상세 조회",
    responses={404: {"model": ErrorResponse, "description": "공연 없음"}},
)
def get_concert(
    concert_id: Annotated[str, Path(description="공연 ID", examples=["1"])],
):
    perform_id = parse_perform_id(concert_id)
    with get_connection() as conn:
        row = fetch_perform(conn, perform_id)
    return row_to_concert(row)


# --- 좌석 API ---


@app.get(
    "/api/concerts/{concert_id}/seats",
    response_model=SeatListResponse,
    tags=["좌석"],
    summary="좌석 목록 조회",
    responses={404: {"model": ErrorResponse, "description": "공연 또는 좌석 정보 없음"}},
)
def list_seats(
    concert_id: Annotated[str, Path(description="공연 ID", examples=["1"])],
):
    perform_id = parse_perform_id(concert_id)
    with get_connection() as conn:
        fetch_perform(conn, perform_id)
        rows = conn.execute(
            """
            SELECT seat_id, seat_no, status
            FROM seat_status
            WHERE perform_id = %s
            ORDER BY seat_no
            """,
            (perform_id,),
        ).fetchall()

    if not rows:
        raise HTTPException(status_code=404, detail="좌석 정보를 찾을 수 없습니다.")

    return SeatListResponse(
        concertId=concert_id,
        seats=[row_to_seat(row) for row in rows],
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
        403: {"model": ErrorResponse, "description": "대기열 입장 권한 없음"},
        404: {"model": ErrorResponse, "description": "좌석 없음"},
        409: {"model": ErrorResponse, "description": "이미 예매된 좌석"},
    },
)
def create_booking(
    body: CreateBookingRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    perform_id = parse_perform_id(body.concertId)
    user_id = int(current_user["id"])

    with get_connection() as conn:
        perform = fetch_perform(conn, perform_id)

        if perform["status"] != "OPEN":
            raise HTTPException(status_code=400, detail="예매가 불가능한 공연입니다.")

        now = datetime.now(timezone.utc)
        opens_at = perform["booking_opens_at"]
        closes_at = perform["booking_closes_at"]
        if opens_at.tzinfo is None:
            opens_at = opens_at.replace(tzinfo=timezone.utc)
        if closes_at.tzinfo is None:
            closes_at = closes_at.replace(tzinfo=timezone.utc)
        if not (opens_at <= now <= closes_at):
            raise HTTPException(status_code=400, detail="예매 가능 시간이 아닙니다.")

        if requires_queue_admission(str(perform_id), str(user_id), perform, now):
            require_queue_admission(str(perform_id), str(user_id))

        existing_count = conn.execute(
            "SELECT COUNT(*) AS count FROM booking WHERE perform_id = %s AND user_id = %s",
            (perform_id, user_id),
        ).fetchone()["count"]
        if existing_count + len(body.seatIds) > perform["max_tickets_per_user"]:
            raise HTTPException(
                status_code=400,
                detail=f"최대 {perform['max_tickets_per_user']}매까지 예매할 수 있습니다.",
            )

        try:
            with conn.transaction():
                seats = conn.execute(
                    """
                    SELECT seat_id, seat_no, status
                    FROM seat_status
                    WHERE perform_id = %s AND seat_no = ANY(%s)
                    FOR UPDATE
                    """,
                    (perform_id, body.seatIds),
                ).fetchall()

                seat_map = {row["seat_no"]: row for row in seats}
                for seat_id in body.seatIds:
                    seat = seat_map.get(seat_id)
                    if not seat:
                        raise HTTPException(
                            status_code=404,
                            detail=f"좌석 '{seat_id}'을(를) 찾을 수 없습니다.",
                        )
                    if seat["status"] != "AVAILABLE":
                        raise HTTPException(
                            status_code=409,
                            detail=f"좌석 '{seat_id}'은(는) 이미 예매되었습니다.",
                        )

                booking_ids: list[int] = []
                for seat_id in body.seatIds:
                    seat = seat_map[seat_id]
                    booking_row = conn.execute(
                        """
                        INSERT INTO booking (perform_id, user_id, seat_id)
                        VALUES (%s, %s, %s)
                        RETURNING booking_id, booked_at
                        """,
                        (perform_id, user_id, seat["seat_id"]),
                    ).fetchone()
                    booking_ids.append(booking_row["booking_id"])
                    conn.execute(
                        "UPDATE seat_status SET status = 'BOOKED' WHERE seat_id = %s",
                        (seat["seat_id"],),
                    )
        except HTTPException:
            raise
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":
                raise HTTPException(status_code=409, detail="이미 예매된 좌석이 있습니다.") from exc
            raise

        group_rows = conn.execute(
            """
            SELECT b.booking_id, b.user_id, b.perform_id, b.booked_at,
                   p.perform_name, s.seat_no
            FROM booking b
            JOIN perform_info p ON p.perform_id = b.perform_id
            JOIN seat_status s ON s.seat_id = b.seat_id
            WHERE b.booking_id = ANY(%s)
            ORDER BY s.seat_no
            """,
            (booking_ids,),
        ).fetchall()

    if has_queue_admission(str(perform_id), str(user_id)):
        consume_queue_admission(str(perform_id), str(user_id))

    return rows_to_booking(group_rows)


@app.get(
    "/api/bookings/me",
    response_model=BookingListResponse,
    tags=["예매"],
    summary="내 예매 내역 조회",
    responses={401: {"model": ErrorResponse, "description": "인증 필요"}},
)
def list_my_bookings(current_user: Annotated[dict, Depends(get_current_user)]):
    user_id = int(current_user["id"])
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT b.booking_id, b.user_id, b.perform_id, b.booked_at,
                   p.perform_name, s.seat_no
            FROM booking b
            JOIN perform_info p ON p.perform_id = b.perform_id
            JOIN seat_status s ON s.seat_id = b.seat_id
            WHERE b.user_id = %s
            ORDER BY b.booked_at DESC, b.booking_id DESC
            """,
            (user_id,),
        ).fetchall()

    grouped: dict[tuple, list[dict]] = {}
    for row in rows:
        key = booking_group_key(row)
        grouped.setdefault(key, []).append(row)

    bookings = [rows_to_booking(group) for group in grouped.values()]
    bookings.sort(key=lambda item: item.createdAt, reverse=True)
    return BookingListResponse(bookings=bookings)


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
    match = re.fullmatch(r"booking-(\d+)", booking_id)
    if not match:
        raise HTTPException(status_code=404, detail="예매 내역을 찾을 수 없습니다.")

    primary_id = int(match.group(1))
    user_id = int(current_user["id"])

    with get_connection() as conn:
        anchor = conn.execute(
            """
            SELECT b.booking_id, b.user_id, b.perform_id, b.booked_at
            FROM booking b
            WHERE b.booking_id = %s
            """,
            (primary_id,),
        ).fetchone()
        if not anchor:
            raise HTTPException(status_code=404, detail="예매 내역을 찾을 수 없습니다.")
        if anchor["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")

        booked_at = anchor["booked_at"].replace(microsecond=0)
        rows = conn.execute(
            """
            SELECT b.booking_id, b.user_id, b.perform_id, b.booked_at,
                   p.perform_name, s.seat_no
            FROM booking b
            JOIN perform_info p ON p.perform_id = b.perform_id
            JOIN seat_status s ON s.seat_id = b.seat_id
            WHERE b.user_id = %s
              AND b.perform_id = %s
              AND date_trunc('second', b.booked_at) = date_trunc('second', %s::timestamptz)
            ORDER BY s.seat_no
            """,
            (anchor["user_id"], anchor["perform_id"], booked_at),
        ).fetchall()

    return rows_to_booking(rows)
