## Scenario runners

The suite is built around a few small scenario runners:

- `PoolHealthScenario` — exercises `evaluate_pool_health` only (warmup, EMA,
  reject thresholds, backoff timers).
- `PlannerScenario` — runs the rules pipeline and `plan_tick` against an
  in-memory snapshot.
- `ExecutorScenario` — applies an `AllocationPlan` through `PlanExecutor` and
  asserts on DB updates and published commands.
- `AllocatorScenario` — runs an end-to-end allocator tick (health + rules +
  planner + executor) using in-memory Redis/Postgres doubles.

## Time control

Every scenario uses `DeterministicClock`. Tests control time explicitly:

- `.at(datetime(...))` freezes time
- `.advance(seconds=...)` moves time forward

## Modelling miner pool switches

IRL Miners switch pools asynchronously after the proxy publishes a
command. `AllocatorScenario.apply_new_pubsub_commands()` is an explicit step
that applies newly published commands to the simulated miners. 
