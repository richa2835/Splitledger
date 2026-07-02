// k6 load test for SplitLedger.
//
// Install k6: https://k6.io/docs/get-started/installation/
// Run:        k6 run load-test.js
// (Make sure the API is running first: docker-compose up)
//
// This ramps virtual users up, holds steady, then ramps down, hitting a mix
// of writes (/transfer) and reads (/accounts/{id}/balance). Results (req/s,
// p50/p95/p99 latency, error rate) print to the terminal when it finishes --
// copy those numbers into the README.

import http from "k6/http";
import { check, sleep } from "k6";
import { Counter, Trend } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";

const errorCount = new Counter("errors");
const transferDuration = new Trend("transfer_duration");
const balanceDuration = new Trend("balance_duration");

export const options = {
  stages: [
    { duration: "20s", target: 20 },   // ramp up to 20 VUs
    { duration: "40s", target: 20 },   // hold at 20 VUs
    { duration: "20s", target: 50 },   // spike to 50 VUs
    { duration: "20s", target: 0 },    // ramp down
  ],
  thresholds: {
    http_req_duration: ["p(95)<500"],   // p95 under 500ms
    errors: ["count<50"],
  },
};

// Pre-created in setup(): two accounts to transfer between, reused by all VUs.
export function setup() {
  const senderRes = http.post(
    `${BASE_URL}/users`,
    JSON.stringify({ name: "LoadTestSender", email: `loadtest-sender-${Date.now()}@test.com` }),
    { headers: { "Content-Type": "application/json" } }
  );
  const receiverRes = http.post(
    `${BASE_URL}/users`,
    JSON.stringify({ name: "LoadTestReceiver", email: `loadtest-receiver-${Date.now()}@test.com` }),
    { headers: { "Content-Type": "application/json" } }
  );

  const sender = senderRes.json();
  const receiver = receiverRes.json();

  http.post(
    `${BASE_URL}/accounts/${sender.account_id}/deposit`,
    JSON.stringify({ amount: "1000000.00" }),
    { headers: { "Content-Type": "application/json" } }
  );

  return { senderAccountId: sender.account_id, receiverAccountId: receiver.account_id };
}

export default function (data) {
  // 70% reads, 30% writes -- roughly mimics real wallet-app traffic
  // (people check balances a lot more than they send money).
  if (Math.random() < 0.7) {
    const res = http.get(`${BASE_URL}/accounts/${data.senderAccountId}/balance`);
    balanceDuration.add(res.timings.duration);
    const ok = check(res, { "balance status is 200": (r) => r.status === 200 });
    if (!ok) errorCount.add(1);
  } else {
    const key = `k6-${__VU}-${__ITER}-${Date.now()}`;
    const res = http.post(
      `${BASE_URL}/transfer`,
      JSON.stringify({
        from_account: data.senderAccountId,
        to_account: data.receiverAccountId,
        amount: "0.01",
        idempotency_key: key,
      }),
      { headers: { "Content-Type": "application/json" } }
    );
    transferDuration.add(res.timings.duration);
    const ok = check(res, { "transfer status is 200": (r) => r.status === 200 });
    if (!ok) errorCount.add(1);
  }

  sleep(0.1);
}
