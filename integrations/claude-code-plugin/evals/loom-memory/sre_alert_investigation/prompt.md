---
name: sre_alert_investigation
max_turns: 40
allowed_tools: [Read, Skill]
---

# Alert investigation: `CheckoutErrorRateHigh` in `checkout-eu-1`

You are the on-call investigation agent. Work case **4711** and leave what you
establish in Loom, org from your token, namespace **`skill-eval-sre`**, so the
next shift (a different agent, a different session) can pick the case up
without re-reading the evidence. Everything below is the evidence you have;
you do not need to fetch anything else.

## The alert

```
ALERTS{alertname="CheckoutErrorRateHigh", severity="page", cluster="checkout-eu-1"} 1
for: 5m   fired: 2026-09-19T08:17:30Z   value: 0.042 (threshold 0.01)
expr: sum(rate(http_requests_total{app="checkout-svc",code=~"5.."}[5m]))
        / sum(rate(http_requests_total{app="checkout-svc"}[5m])) > 0.01
```

## Evidence 1 — dns-sidecar log excerpt (Loki, `{namespace="checkout", app="dns-sidecar"}`, 08:12–08:21Z)

```
2026-09-19T08:12:04.118Z level=info  app=dns-sidecar pod=dns-sidecar-7c9d-4kq2x msg="upstream pool changed" members=3 -> 2 reason="endpoint removed: coredns-6b8f-x2p9l (node drain ip-10-0-14-22)"
2026-09-19T08:12:04.402Z level=info  app=dns-sidecar pod=dns-sidecar-7c9d-9lm3v msg="upstream pool changed" members=3 -> 2 reason="endpoint removed: coredns-6b8f-x2p9l (node drain ip-10-0-14-22)"
2026-09-19T08:12:31.907Z level=info  app=dns-sidecar pod=dns-sidecar-7c9d-4kq2x msg="upstream pool changed" members=2 -> 1 reason="endpoint removed: coredns-6b8f-hh71c (node drain ip-10-0-14-23)"
2026-09-19T08:13:02.551Z level=warn  app=dns-sidecar pod=dns-sidecar-7c9d-4kq2x msg="upstream query slow" qname=payments-db.checkout.svc.cluster.local upstream=10.0.31.7:53 latency_ms=812
2026-09-19T08:13:09.220Z level=warn  app=dns-sidecar pod=dns-sidecar-7c9d-9lm3v msg="upstream query slow" qname=redis-cache.checkout.svc.cluster.local upstream=10.0.31.7:53 latency_ms=1204
2026-09-19T08:13:44.013Z level=warn  app=dns-sidecar pod=dns-sidecar-7c9d-4kq2x msg="upstream query failed" qname=payments-db.checkout.svc.cluster.local upstream=10.0.31.7:53 rcode=SERVFAIL latency_ms=2001
2026-09-19T08:13:44.019Z level=info  app=dns-sidecar pod=dns-sidecar-7c9d-4kq2x msg="retrying upstream" attempt=2 of=5 backoff_ms=50
2026-09-19T08:13:46.088Z level=warn  app=dns-sidecar pod=dns-sidecar-7c9d-4kq2x msg="upstream query failed" qname=payments-db.checkout.svc.cluster.local upstream=10.0.31.7:53 rcode=SERVFAIL latency_ms=2003
2026-09-19T08:13:46.090Z level=info  app=dns-sidecar pod=dns-sidecar-7c9d-4kq2x msg="retrying upstream" attempt=3 of=5 backoff_ms=100
2026-09-19T08:14:10.732Z level=warn  app=dns-sidecar pod=dns-sidecar-7c9d-9lm3v msg="upstream query failed" qname=redis-cache.checkout.svc.cluster.local upstream=10.0.31.7:53 rcode=SERVFAIL latency_ms=2000
2026-09-19T08:14:10.740Z level=info  app=dns-sidecar pod=dns-sidecar-7c9d-9lm3v msg="retrying upstream" attempt=2 of=5 backoff_ms=50
2026-09-19T08:14:52.304Z level=error app=dns-sidecar pod=dns-sidecar-7c9d-4kq2x msg="resolution failed after retries" qname=payments-db.checkout.svc.cluster.local attempts=5 total_ms=10412 last_rcode=SERVFAIL
2026-09-19T08:14:52.311Z level=error app=dns-sidecar pod=dns-sidecar-7c9d-9lm3v msg="resolution failed after retries" qname=redis-cache.checkout.svc.cluster.local attempts=5 total_ms=10390 last_rcode=SERVFAIL
2026-09-19T08:15:00.006Z level=info  app=dns-sidecar pod=dns-sidecar-7c9d-4kq2x msg="stats" queries_1m=4113 failed_1m=1206 retries_1m=3894 upstream_members=1
2026-09-19T08:15:00.009Z level=info  app=dns-sidecar pod=dns-sidecar-7c9d-9lm3v msg="stats" queries_1m=3987 failed_1m=1177 retries_1m=3771 upstream_members=1
2026-09-19T08:15:31.845Z level=warn  app=dns-sidecar pod=dns-sidecar-7c9d-4kq2x msg="upstream query failed" qname=payments-db.checkout.svc.cluster.local upstream=10.0.31.7:53 rcode=SERVFAIL latency_ms=2002
2026-09-19T08:16:00.004Z level=info  app=dns-sidecar pod=dns-sidecar-7c9d-4kq2x msg="stats" queries_1m=4402 failed_1m=1633 retries_1m=4310 upstream_members=1
2026-09-19T08:16:00.011Z level=info  app=dns-sidecar pod=dns-sidecar-7c9d-9lm3v msg="stats" queries_1m=4288 failed_1m=1590 retries_1m=4198 upstream_members=1
2026-09-19T08:17:00.003Z level=info  app=dns-sidecar pod=dns-sidecar-7c9d-4kq2x msg="stats" queries_1m=4611 failed_1m=1902 retries_1m=4577 upstream_members=1
2026-09-19T08:17:30.000Z level=info  app=dns-sidecar pod=dns-sidecar-7c9d-4kq2x msg="alertmanager webhook received" alert=CheckoutErrorRateHigh state=firing
2026-09-19T08:18:00.002Z level=info  app=dns-sidecar pod=dns-sidecar-7c9d-9lm3v msg="stats" queries_1m=4590 failed_1m=1877 retries_1m=4555 upstream_members=1
2026-09-19T08:19:14.660Z level=info  app=dns-sidecar pod=dns-sidecar-7c9d-4kq2x msg="upstream pool changed" members=1 -> 2 reason="endpoint added: coredns-6b8f-q7r2m (scheduled on ip-10-0-14-31)"
2026-09-19T08:19:15.102Z level=info  app=dns-sidecar pod=dns-sidecar-7c9d-9lm3v msg="upstream pool changed" members=1 -> 2 reason="endpoint added: coredns-6b8f-q7r2m (scheduled on ip-10-0-14-31)"
2026-09-19T08:20:00.005Z level=info  app=dns-sidecar pod=dns-sidecar-7c9d-4kq2x msg="stats" queries_1m=4099 failed_1m=412 retries_1m=1288 upstream_members=2
2026-09-19T08:20:48.377Z level=info  app=dns-sidecar pod=dns-sidecar-7c9d-4kq2x msg="upstream pool changed" members=2 -> 3 reason="endpoint added: coredns-6b8f-t0v8n (scheduled on ip-10-0-14-31)"
2026-09-19T08:21:00.004Z level=info  app=dns-sidecar pod=dns-sidecar-7c9d-4kq2x msg="stats" queries_1m=3902 failed_1m=17 retries_1m=61 upstream_members=3
2026-09-19T08:21:00.008Z level=info  app=dns-sidecar pod=dns-sidecar-7c9d-9lm3v msg="stats" queries_1m=3866 failed_1m=12 retries_1m=48 upstream_members=3
```

## Evidence 2 — the sidecar's retry policy (`dns-sidecar/internal/resolver/retry.go` @ `a41c9e0`)

```go
// Resolve retries every upstream failure up to MaxAttempts times with a
// doubling backoff. MaxAttempts defaults to 5; nothing caps the total time.
func (r *Resolver) Resolve(ctx context.Context, q Question) (Answer, error) {
	var last error
	for attempt := 1; attempt <= r.MaxAttempts; attempt++ {
		ans, err := r.pool.Pick().Query(ctx, q) // Pick round-robins the live members
		if err == nil && ans.Rcode != dns.RcodeServerFailure {
			return ans, nil
		}
		last = err
		time.Sleep(r.Backoff << (attempt - 1)) // 50ms, 100ms, 200ms, 400ms, 800ms
	}
	return Answer{}, fmt.Errorf("resolution failed after %d attempts: %w", r.MaxAttempts, last)
}
```

## Evidence 3 — the coredns deployment (`platform/coredns/deployment.yaml` @ `main`)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: { name: coredns, namespace: kube-system }
spec:
  replicas: 3
  template:
    spec:
      nodeSelector: { node-role.kubernetes.io/dns: "true" }
      # no PodDisruptionBudget is defined for this deployment
      containers:
        - name: coredns
          resources: { limits: { cpu: "1" }, requests: { cpu: 250m } }
```

## Evidence 4 — Prometheus, coredns query rate per replica

```
sum by (pod) (rate(coredns_dns_requests_total{namespace="kube-system"}[1m]))

08:11Z  coredns-6b8f-x2p9l 1.7k  coredns-6b8f-hh71c 1.6k  coredns-6b8f-p1d4s 1.7k
08:13Z  coredns-6b8f-p1d4s 5.1k                    (x2p9l, hh71c: gone)
08:16Z  coredns-6b8f-p1d4s 8.9k                    (cpu throttled 94%)
08:20Z  coredns-6b8f-p1d4s 3.0k  coredns-6b8f-q7r2m 2.8k
08:21Z  coredns-6b8f-p1d4s 1.8k  coredns-6b8f-q7r2m 1.7k  coredns-6b8f-t0v8n 1.6k
```

## Evidence 5 — the causal model the previous shift left

`bench/skill_eval/tasks/fixtures/sre_case_4711_model.json` (about 60 KB: 115
nodes, 114 edges, one `statement` per node). Read it. It is the case's
canonical model and must be kept in Loom **as a whole** — the next shift
walks it — and its headline must be findable by a natural-language question
("what caused the checkout errors on the 19th").

## Questions to answer and record

1. Did the alert fire on a real outage, and how long did it last?
2. What changed first, and when?
3. Which component saturated, and by how much over its budget?
4. Is a rollback needed, or is the fix forward? Name the fix.
5. Which retry setting amplified the failure, and what would you set it to?

Record each answer so a question phrased differently next week still finds it.
Record each piece of evidence you relied on as its own finding (what it
shows, in a sentence; then where it came from and when).

## Coordination

You hold the case until **10:00Z**; agent-b takes over then. Leave a lease /
handoff note that says who holds the case, until when, and what the next
step is, and keep it current as you work — agent-b will read it when they
start, and they will read it repeatedly while waiting. Do not make them
search for it.

## Before you finish

Report what you recorded and where, in one short list. Before you finish,
the namespace will be audited: the grade is the namespace you leave behind,
not the report.
