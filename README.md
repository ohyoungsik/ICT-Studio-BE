### ICT-Studio-BE

FastAPI backend for ICT Studio ticketing.

```bash
source .venv/bin/activate


windows

python -m venv .venv

.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt

uvicorn api:app --reload
```

## Health check

```bash
curl http://localhost:8000/health
```

예상 응답:

```json
{"status":"ok"}
```

## Redis 대기열 및 k6 부하 테스트 API

대기열 API는 Redis가 필요합니다.
현재 백엔드는 PostgreSQL을 사용하지 않습니다. `/api/reservations`는 부하 테스트 중
예약 완료 응답을 흉내내기 위한 인메모리 API이며, 백엔드 프로세스가 재시작되면
데이터가 초기화됩니다.

```bash
docker compose up --build
```

AWS 환경에서는 infra 프로젝트가 Swarm manager 노드에서 Redis를 실행하고,
bootstrap script가 SSM Parameter Store의 값을 읽어 백엔드 컨테이너에 아래
환경변수를 주입합니다.

```text
REDIS_HOST
REDIS_PORT
REDIS_PASSWORD
MAX_QUEUE_SIZE
```

제공 API:

```text
POST /api/queue/join
GET  /api/queue/status/{concertId}/{userId}
GET  /api/queue/length/{concertId}
POST /api/reservations
```

대기열 등록 요청 예시:

```bash
curl -X POST http://localhost:8000/api/queue/join \
  -H "Content-Type: application/json" \
  -d '{"concertId":1,"userId":"user-1"}'
```

k6 대기열 부하 테스트 실행:

```bash
k6 run -e BASE_URL=http://localhost:8000 k6/ticketing-load-test.js
```

대기열은 Redis Sorted Set으로 저장되며 key 형식은
`queue:concert:{concertId}:zset`입니다. 기존 Redis List 테스트 key인
`queue:concert:{concertId}`와 충돌하지 않도록 별도 key를 사용합니다.

응답 상태 코드는 다음 기준으로 구분합니다.

```text
202: 신규 사용자가 대기열에 등록됨
200: 이미 등록된 사용자가 기존 순번을 조회함
429: MAX_QUEUE_SIZE를 초과하여 대기열 등록이 거절됨
503: Redis 연결 또는 명령 처리 실패
```

## Docker

로컬 Docker 이미지 빌드 및 실행:

```bash
docker build -t ict-studio-be .

docker run -d \
  --name ict-studio-be \
  -p 8000:8000 \
  ict-studio-be

curl http://localhost:8000/health
docker logs ict-studio-be
docker rm -f ict-studio-be
```

Docker Hub 이미지 pull 및 실행:

```bash
docker pull ohyoungsik/ict-studio-be:latest

docker run -d \
  --name ict-studio-be \
  -p 8000:8000 \
  ohyoungsik/ict-studio-be:latest

curl http://localhost:8000/health
docker rm -f ict-studio-be
```

## CI/CD

GitHub Actions는 아래 태그로 Docker 이미지를 빌드하고 push합니다.

```text
ohyoungsik/ict-studio-be:latest
ohyoungsik/ict-studio-be:<github-sha>
```

필수 secrets:

```text
DOCKERHUB_USERNAME
DOCKERHUB_TOKEN
```

선택 배포 secrets 또는 variables:

```text
AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY
AWS_REGION
ASG_NAME
```

`ASG_NAME`이 설정되어 있지 않으면 workflow는 Docker 이미지 빌드와 push까지만
수행합니다. 이 경우 AWS ASG 리소스가 없어도 백엔드 이미지 빌드 파이프라인은
사용할 수 있습니다.
