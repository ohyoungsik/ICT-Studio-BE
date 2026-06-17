import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter, Rate } from 'k6/metrics';

const statusCodes = new Counter('status_codes');
const serverErrors = new Rate('server_errors');

export const options = {
  stages: [
    { duration: '1m', target: 5 },
    { duration: '5m', target: 20 },
    { duration: '1m', target: 0 },
  ],
  thresholds: {
    http_req_duration: ['p(95)<2000'],
    checks: ['rate>0.99'],
    server_errors: ['rate<0.01'],
  },
};

export default function () {
  const baseUrl = __ENV.BASE_URL || 'http://localhost:8000';
  const concertId = __ENV.CONCERT_ID || '1';
  const count = Number(__ENV.PROCESS_COUNT || '10');

  const payload = JSON.stringify({
    concertId,
    count,
  });

  const params = {
    headers: {
      'Content-Type': 'application/json',
    },
  };

  const res = http.post(`${baseUrl}/api/queue/process`, payload, params);
  statusCodes.add(1, { status: String(res.status) });
  serverErrors.add(res.status >= 500 || res.status === 0);

  check(res, {
    'queue consumed or empty': (r) => r.status === 200,
    'no server error': (r) => r.status < 500,
  });

  sleep(1);
}
