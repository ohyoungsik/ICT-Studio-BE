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

Expected response:

```json
{"status":"ok"}
```

## Queue and reservation load-test APIs

Redis is required for queue APIs.
PostgreSQL is not required by the current backend implementation. The
`/api/reservations` endpoint is a lightweight in-memory endpoint for load-test
response simulation, so data is not persisted after the backend process restarts.

```bash
docker compose up --build
```

In AWS, the infra project starts Redis on the swarm manager and passes these
values into the backend container through SSM-backed bootstrap scripts:

```text
REDIS_HOST
REDIS_PORT
REDIS_PASSWORD
```

Available endpoints:

```text
POST /api/queue/join
GET  /api/queue/status/{concertId}/{userId}
GET  /api/queue/length/{concertId}
POST /api/reservations
```

Example queue request:

```bash
curl -X POST http://localhost:8000/api/queue/join \
  -H "Content-Type: application/json" \
  -d '{"concertId":1,"userId":"user-1"}'
```

Run the k6 queue load test:

```bash
k6 run -e BASE_URL=http://localhost:8000 k6/ticketing-load-test.js
```

## Docker

Build and run locally:

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

Pull and run the Docker Hub image:

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

GitHub Actions builds and pushes these tags:

```text
ohyoungsik/ict-studio-be:latest
ohyoungsik/ict-studio-be:<github-sha>
```

Required secrets:

```text
DOCKERHUB_USERNAME
DOCKERHUB_TOKEN
```

Optional deployment secrets or variables:

```text
AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY
AWS_REGION
ASG_NAME
```

If `ASG_NAME` is not set, the workflow only builds and pushes the Docker image. This keeps the backend pipeline usable while AWS ASG resources are not present.
