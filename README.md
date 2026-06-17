### ICT-Studio-BE

FastAPI backend for ICT Studio ticketing.

```bash
source .venv/bin/activate

# Windows
python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
uvicorn api:app --reload
```

## Health Check

```bash
curl http://localhost:8000/health
```

예상 응답:

```json
{"status":"ok"}
```

Redis 연결 상태는 별도 endpoint에서 확인합니다.

```bash
curl http://localhost:8000/api/health/redis
```

## Redis 대기열 및 k6 부하 테스트 API

대기열 API는 Redis가 필요합니다. 현재 백엔드는 PostgreSQL을 사용하지 않습니다.
`/api/reservations`는 부하 테스트 중 예약 완료 응답을 흉내내기 위한 인메모리 API이며,
백엔드 프로세스가 재시작되면 데이터가 초기화됩니다.

로컬에서는 아래 명령으로 백엔드와 Redis를 함께 실행할 수 있습니다.

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
POST /api/queue/process
POST /api/queue/worker
GET  /api/queue/length
GET  /api/queue/status/{concertId}/{userId}
GET  /api/queue/length/{concertId}
POST /api/reservations
```

## Redis Key 계약

인프라는 Redis 대기열 길이를 CloudWatch custom metric으로 발행하고, 이 값을 기준으로
Auto Scaling Group을 scale-out 합니다. 백엔드는 인프라가 읽을 수 있도록 아래 key를
유지해야 합니다.

기본 테스트 대상 concert ID는 `1`입니다.

```text
queue:concert:1:zset
```

권장 자료구조는 Redis Sorted Set입니다.

```redis
ZCARD queue:concert:1:zset
```

일반화된 key 형식:

```text
대기열: queue:concert:{concertId}:zset
순번:   queue:concert:{concertId}:seq
```

사용 명령:

```redis
ZRANK queue:concert:{concertId}:zset {userId}
ZCARD queue:concert:{concertId}:zset
INCR  queue:concert:{concertId}:seq
ZADD  queue:concert:{concertId}:zset {seq} {userId}
ZPOPMIN queue:concert:{concertId}:zset {count}
```

마이그레이션 기간에는 infra가 구버전 list key도 fallback으로 읽을 수 있습니다.
운영 기준 최종 구조는 Sorted Set입니다.

```text
구버전 key: queue:concert:{concertId}
fallback:  LLEN queue:concert:{concertId}
```

## Queue Join 동작

대기열 등록 요청 예시:

```bash
curl -X POST http://localhost:8000/api/queue/join \
  -H "Content-Type: application/json" \
  -d '{"concertId":1,"userId":"user-1"}'
```

`/api/queue/join`은 전체 큐를 조회하지 않습니다. `LRANGE 0 -1`, `KEYS *` 같은
전체 조회 명령은 hot path에서 사용하지 않습니다.

동작 기준:

```text
이미 등록된 userId면 기존 순번 반환
신규 userId면 Redis Sorted Set에 등록 후 순번 반환
대기열 최대치 초과 시 429 반환
Redis 장애 시 503 반환
```

응답 상태 코드:

```text
202: 신규 사용자가 대기열에 등록됨
200: 이미 등록된 사용자가 기존 순번을 조회함
429: MAX_QUEUE_SIZE를 초과하여 대기열 등록이 거절됨
503: Redis 연결 또는 명령 처리 실패
```

`MAX_QUEUE_SIZE` 기본값은 `10000`입니다.

`concertId`를 생략하면 기본 검증 큐인 `concertId=1`에 등록됩니다.
따라서 Scale-Out 검증용 단순 요청은 아래처럼 보낼 수 있습니다.

```bash
curl -X POST http://localhost:8000/api/queue/join \
  -H "Content-Type: application/json" \
  -d '{"userId":"load-user-1"}'
```

## Queue Status / Length

`/api/queue/status/{concertId}/{userId}`는 전체 큐 조회 없이 Redis `ZRANK`와 `ZCARD`로
사용자 위치와 큐 길이를 계산합니다.

```python
rank = redis_client.zrank(key, user_id)
queue_length = redis_client.zcard(key)
position = rank + 1 if rank is not None else None
```

`/api/queue/length/{concertId}`는 Redis `ZCARD`로 큐 길이를 조회합니다.

Scale-Out 검증에서 사용하는 기본 큐 길이는 `/api/queue/length`로도 조회할 수 있습니다.
응답은 CloudWatch publisher나 부하 테스트 확인에 바로 쓰기 쉬운 단순 형태입니다.

```json
{
  "queueLength": 8432
}
```

기본 큐는 `DEFAULT_CONCERT_ID` 환경 변수로 바꿀 수 있으며, 기본값은 `1`입니다.

## Queue Process / Worker

Scale-In 검증을 위해 Redis Sorted Set에서 대기 사용자를 소비하는 API를 제공합니다.
`/api/queue/process`와 `/api/queue/worker`는 같은 동작을 수행하며, `ZPOPMIN`으로 가장 먼저 등록된 사용자부터 제거합니다.

```bash
curl -X POST http://localhost:8000/api/queue/process \
  -H "Content-Type: application/json" \
  -d '{"concertId":1,"count":100}'
```

`concertId`를 생략하면 기본 검증 큐인 `concertId=1`을 소비합니다.

```bash
curl -X POST http://localhost:8000/api/queue/worker \
  -H "Content-Type: application/json" \
  -d '{"count":100}'
```

응답 예시:

```json
{
  "status": "PROCESSED",
  "concertId": 1,
  "processedCount": 100,
  "queueLength": 9900,
  "users": [
    {
      "userId": "user-1",
      "score": 1
    }
  ],
  "message": "Queue users processed."
}
```

대기열이 비어 있으면 `processedCount`는 `0`, `status`는 `EMPTY`로 반환됩니다.
이 API 호출로 `ZCARD queue:concert:1:zset` 값이 감소하면 infra의 metric publisher가 낮아진 Queue Length를 CloudWatch에 발행하고, ASG Scale-In 정책을 검증할 수 있습니다.

## CloudWatch Metric 책임

백엔드는 요청 처리 경로에서 CloudWatch `PutMetricData`를 호출하지 않습니다.

CloudWatch metric 발행은 infra의 master node publisher가 담당합니다. 백엔드는
Redis key를 안정적으로 유지하는 역할만 합니다.

인프라 발행 metric:

```text
Namespace: ICT/Queue
MetricName: QueueLength
MetricName: QueueLengthPerInstance
MetricName: QueueLengthPerInstanceForAsg
Dimensions:
- Environment=prod
- ConcertId=1
```

ASG target tracking policy는 dimension 없는 `QueueLengthPerInstanceForAsg` metric을
사용합니다.

## k6 부하 테스트

k6 대기열 부하 테스트 실행:

```bash
k6 run -e BASE_URL=http://localhost:8000 k6/ticketing-load-test.js
```

ALB 대상 실행 예시:

```bash
k6 run --vus 500 --duration 3m \
  -e BASE_URL=http://prod-ict-alb-469671516.ap-northeast-2.elb.amazonaws.com \
  k6/ticketing-load-test.js
```

Scale-In 검증용 Queue 소비 부하 테스트:

```bash
k6 run \
  -e BASE_URL=http://localhost:8000 \
  -e CONCERT_ID=1 \
  -e PROCESS_COUNT=10 \
  k6/queue-consume-test.js
```

단건 API 호출로 Queue를 줄이는 명령:

```bash
curl -X POST http://localhost:8000/api/queue/process \
  -H "Content-Type: application/json" \
  -d '{"concertId":1,"count":100}'
```

대기열 초과 정책을 테스트할 때 `429`는 의도된 응답일 수 있습니다. 따라서 k6 check는
`200`, `202`, `429`를 기대 가능한 응답으로 분류하고, `server_errors` custom metric으로
5xx/timeout을 따로 봅니다.

## 인프라 확인 명령

Redis에서 큐 길이 확인:

```bash
docker exec redis redis-cli -a "$REDIS_PASSWORD" ZCARD queue:concert:1:zset
```

CloudWatch metric 확인:

```powershell
aws cloudwatch get-metric-statistics `
  --namespace ICT/Queue `
  --metric-name QueueLengthPerInstance `
  --dimensions Name=Environment,Value=prod Name=ConcertId,Value=1 `
  --statistics Average `
  --period 60 `
  --start-time 2026-06-17T07:00:00Z `
  --end-time 2026-06-17T07:20:00Z `
  --region ap-northeast-2
```

ASG target tracking용 dimension 없는 metric 확인:

```powershell
aws cloudwatch get-metric-statistics `
  --namespace ICT/Queue `
  --metric-name QueueLengthPerInstanceForAsg `
  --statistics Average `
  --period 60 `
  --start-time 2026-06-17T07:00:00Z `
  --end-time 2026-06-17T07:20:00Z `
  --region ap-northeast-2
```

ASG scale-out 확인:

```powershell
aws autoscaling describe-scaling-activities `
  --auto-scaling-group-name prod-ict-app-asg `
  --region ap-northeast-2 `
  --max-items 10
```

Scale-In 검증용 Queue 소비:

```powershell
curl.exe -X POST http://prod-ict-alb-469671516.ap-northeast-2.elb.amazonaws.com/api/queue/process `
  -H "Content-Type: application/json" `
  -d "{\"concertId\":1,\"count\":100}"
```

Queue Length 감소 확인:

```powershell
curl.exe http://prod-ict-alb-469671516.ap-northeast-2.elb.amazonaws.com/api/queue/length
```

ASG scale-in 활동 확인:

```powershell
aws autoscaling describe-scaling-activities `
  --auto-scaling-group-name prod-ict-app-asg `
  --region ap-northeast-2 `
  --max-items 10
```

성공 기준:

```text
k6 요청 증가
-> Redis ZCARD 증가
-> CloudWatch ICT/Queue QueueLengthPerInstance 증가
-> ASG desired capacity 증가
-> 새 app instance InService
```

## Scale-Out 검증 결과

Scale-Out 검증은 Redis Sorted Set 적재 흐름으로 완료했습니다.
`/api/queue/join` 호출이 증가하면 `queue:concert:1:zset`의 `ZCARD` 값이 증가하고,
infra의 CloudWatch metric publisher가 이 값을 기반으로 ASG Scale-Out을 검증합니다.

검증된 흐름:

```text
k6 부하 발생
-> POST /api/queue/join
-> Redis Sorted Set queue length 증가
-> QueueLength / QueueLengthPerInstance metric 증가
-> ASG 1 instance에서 4 instance까지 Scale-Out
```

확인된 Queue Length:

```text
0 -> 10000
```

Scale-In 검증은 `/api/queue/process` 또는 `/api/queue/worker`로 Queue Length를 감소시킨 뒤 진행합니다.

```text
POST /api/queue/process
-> Redis ZPOPMIN 수행
-> Queue Length 감소
-> QueueLength / QueueLengthPerInstance metric 감소
-> ASG Scale-In 검증
```

인프라에서 자동 Queue consumer를 사용하는 경우에는 검증 단계에 따라 consumer를 켜고 끕니다.

```text
Scale-Out 검증 단계:
enable_queue_consumer = false
-> Queue가 자동 소비되지 않도록 유지
-> k6 join 부하로 Queue Length를 10000까지 증가
-> ASG Scale-Out 확인

Scale-In 검증 단계:
enable_queue_consumer = true
또는 POST /api/queue/process 호출
-> Queue Length 감소
-> CloudWatch metric 감소
-> ASG Scale-In 확인
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
