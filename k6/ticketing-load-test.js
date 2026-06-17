import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter } from 'k6/metrics';

const statusCodes = new Counter('status_codes');

export const options = {
  stages: [
    { duration: '3m', target: 20 },
    { duration: '5m', target: 100 },
    { duration: '3m', target: 20 },
    { duration: '2m', target: 0 },
  ],
  thresholds: {
    http_req_failed: ['rate<0.05'],
    http_req_duration: ['p(95)<2000'],
  },
};

export default function () {
  const baseUrl = __ENV.BASE_URL || 'http://localhost:8000';

  const payload = JSON.stringify({
    concertId: 1,
    userId: `user-${__VU}-${__ITER}`,
  });

  const params = {
    headers: {
      'Content-Type': 'application/json',
    },
  };

  const res = http.post(`${baseUrl}/api/queue/join`, payload, params);
  statusCodes.add(1, { status: String(res.status) });

  check(res, {
    'queued accepted or full': (r) => r.status === 200 || r.status === 202 || r.status === 429,
    'no server error': (r) => r.status < 500,
  });

  sleep(1);
}
