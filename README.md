# QuantEdge Foundation

**A read-only cryptocurrency market-data foundation.**

QuantEdge Foundation v0.1 is designed as a low-latency-oriented market-data ingestion and display scaffold. It is deliberately not a live trading system and does not include order execution.

## Project status

The located v0.1 archive contains a FastAPI backend, a public Binance WebSocket worker, Redis cache/Pub/Sub, PostgreSQL schema and Alembic migration, a Next.js dashboard starter, Docker Compose configuration, tests, and CI workflow. The source tree still needs to be imported and verified in this repository.

## Safety boundary

- Public market data only.
- No exchange API keys required.
- No order placement, cancellation, signing, or withdrawal functionality.
- No live trading or investment recommendation functionality.
- Do not add exchange execution credentials to this foundation.

## Intended architecture

```text
Public exchange WebSocket
          |
          v
    Market data worker
       /         \
      v           v
   Redis       PostgreSQL
      |
      v
    FastAPI
      |
      v
  Next.js dashboard
```

## Intended repository layout

```text
backend/       FastAPI service, worker, migrations and tests
dashboard/     Next.js dashboard
docs/          Architecture, roadmap and security notes
.github/       CI workflows
docker-compose.yml
.env.example
```

## Development prerequisites

- Docker Engine and Docker Compose plugin
- Git
- Optional: Python 3.12+ and Node.js 20+

The exact commands will be finalized after source import and dependency verification.

## Known scope limits

This foundation is not a production trading platform. Market snapshots are not executable quotes; Redis Pub/Sub is not durable; historical backfill, order-book reconstruction, strategy execution, paper trading, risk controls, and live execution are outside v0.1.

## Security

Never expose private exchange credentials to the browser. Keep secrets in local environment files or a managed secret store. Live execution, if ever developed, must be a separately gated service with independent risk controls and security review.

## License

No license has been selected yet. All rights remain with the project owner; do not assume permission to reuse or redistribute this code.
