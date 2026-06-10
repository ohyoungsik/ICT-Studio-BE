from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Path, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field

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
