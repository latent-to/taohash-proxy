# Marketplace Allocator

The TaoHash marketplace lets buyers place orders for mining hashrate at a BTC price per EH/day. Three async loops in the Market Service coordinate the system: the **allocator** decides which miners go where, **share billing** debits order balances in real time, and the **hashprice monitor** tracks the current market rate. Shares flow through Redis Streams, orders and assignments live in PostgreSQL, and share analytics land in ClickHouse.

---

## System Architecture

```
                          ┌──────────────────────────┐
                          │         MINERS            │
                          │  (Stratum connections)    │
                          └────────────┬─────────────┘
                                       │
                              ports 3331 / 3332
                                       │
                          ┌────────────▼─────────────┐
                          │         HAProxy           │
                          │    (leastconn balancer)   │
                          └────────────┬─────────────┘
                                       │
              ┌────────────────────────┼────────────────────────┐
              │                        │                        │
      ┌───────▼───────┐       ┌───────▼───────┐       ┌───────▼───────┐
      │   Proxy #1    │       │   Proxy #2    │  ...  │   Proxy #12   │
      │  MinerSession │       │  MinerSession │       │  MinerSession │
      └──┬────┬────┬──┘       └──┬────┬────┬──┘       └──┬────┬────┬──┘
         │    │    │              │    │    │              │    │    │
         │    │    │              │    │    │              │    │    │
    ┌────┘    │    └────┐   ┌────┘    │    └────┐   ┌────┘    │    └────┐
    │         │         │   │         │         │   │         │         │
    ▼         ▼         ▼   ▼         ▼         ▼   ▼         ▼         ▼
 ┌──────┐ ┌──────┐ ┌──────────────────────────────────────────────────────┐
 │  CH  │ │Redis │ │                     Redis                            │
 │      │ │ hb   │ │                                                      │
 └──┬───┘ └──┬───┘ │  pub/sub: miner:commands     streams: order_shares  │
    │         │     │  key:     market:hashprice    key: proxy:{id}:miners│
    │         │     └───────────────┬──────────────────────┬──────────────┘
    │         │                     │                      │
    │         │                     │                      │
    │    ┌────┘         ┌───────────┴──────────────────────┘
    │    │              │
    │    │    ┌─────────▼──────────────────────────────────────────────┐
    │    │    │              MARKET SERVICE (:8001)                    │
    │    │    │                                                        │
    │    │    │  ┌──────────────────┐  ┌───────────────────────────┐  │
    │    │    │  │  Allocator Loop  │  │   Share Billing Loop      │  │
    │    │    │  │  (every 30s)     │  │   (every 30s)             │  │
    │    │    │  │                  │  │                           │  │
    │    │    │  │  1. read miners  │  │  1. consume order_shares  │  │
    │    │    │  │     from Redis   │  │     stream from Redis     │  │
    │    │    │  │  2. read orders  │  │  2. aggregate difficulty  │  │
    │    │    │  │     from PG      │  │  3. convert to BTC cost   │  │
    │    │    │  │  3. rank by      │  │  4. debit order balance   │  │
    │    │    │  │     price DESC   │  │     in PG                 │  │
    │    │    │  │  4. evaluate     │  │  5. if balance = 0:       │  │
    │    │    │  │     pool health  │  │     fulfill & drain       │  │
    │    │    │  │  5. plan moves   │  │                           │  │
    │    │    │  │  6. execute:     │  └───────────────────────────┘  │
    │    │    │  │     update PG +  │                                  │
    │    │    │  │     publish      │  ┌───────────────────────────┐  │
    │    │    │  │     switch_pool  │  │  Hashprice Monitor        │  │
    │    │    │  │     to Redis     │  │  (every 5min)             │  │
    │    │    │  │                  │  │                           │  │
    │    │    │  └──────────────────┘  │  fetches difficulty +     │  │
    │    │    │                        │  BTC price from external  │  │
    │    │    │                        │  APIs, caches in Redis    │  │
    │    │    │                        └───────────────────────────┘  │
    │    │    └──────────┬───────────────────────┬────────────────────┘
    │    │               │                       │
    │    │               │ switch_pool           │ read/write
    │    │               │ via Redis pub/sub     │ orders & assignments
    │    │               │                       │
    │    │               ▼                       ▼
    │    │    ┌─────────────────────┐  ┌──────────────────────┐
    │    │    │   Proxy receives    │  │     PostgreSQL       │
    │    │    │   switch command    │  │                      │
    │    │    │   via CommandListener  │  marketplace_orders  │
    │    │    └─────────┬───────────┘  │  miner_assignments   │
    │    │              │              └──────────┬───────────┘
    │    │              │                         │
    │    │              │ proxy routes            │ REST API reads/writes
    │    │              │ miner connection        │
    │    │              ▼                         ▼
    │    │    ┌─────────────────────┐  ┌──────────────────────┐
    │    │    │  Buyer's Upstream   │  │   REST API (:8000)   │
    │    │    │  Pool (order host)  │  │   /api/marketplace   │
    │    │    │                     │  │                      │
    │    │    │  miner sends shares │  │  create/list/update  │
    │    │    │  pool sends jobs    │  │  pause/resume/cancel │
    │    │    └─────────────────────┘  │  assign/remove miner │
    │    │                             └──────────────────────┘
    │    │
    ▼    ▼
 ┌──────────────────────┐
 │     ClickHouse       │
 │                      │
 │  shares              │
 │  rejected_shares     │
 │  worker_stats_mv     │
 │  daily_rewards       │
 │                      │
 │  (hot: local disk)   │
 │  (cold: S3 archive)  │
 └──────────────────────┘
```

### The Loop

```
 Allocator publishes             Proxy switches              Miner hashes on
 switch_pool command  ──────►   miner connection  ──────►   buyer's upstream pool
        ▲                                                          │
        │                                                   shares flow back
        │                                                          │
        │                                                          ▼
 Allocator reads             PG order balance             Share Billing debits
 updated state     ◄──────  updated by billing  ◄──────  from Redis stream
```

---

For details on the pool health state machine (how upstream pools are monitored and excluded), see [POOL_HEALTH.md](POOL_HEALTH.md).
