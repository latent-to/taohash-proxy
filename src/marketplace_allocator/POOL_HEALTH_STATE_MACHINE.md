# Pool Health State Machine

This document describes the state machine used by `pool_health.py` to monitor upstream mining pool health and manage order eligibility.

---

## Overview

The pool health system monitors upstream pools by analyzing:
- **Share statistics** (accepted/rejected counts, reject breakdown)
- **Job notifications** (time since last job from pool)

When a pool shows signs of failure, the system excludes the order from allocation until it recovers.

---

## States

| State | Description | Eligible? | Hashrate Cap |
|-------|-------------|-----------|--------------|
| **HEALTHY** | Pool functioning normally | Yes | None |
| **WARMUP** | New order, waiting for initial samples | Yes | None |
| **DEGRADED** | High reject rate, but still receiving hashrate | Yes | None |
| **BACKOFF** | Unhealthy, temporarily excluded | **No** | N/A |
| **PROBATION** | Careful re-entry after backoff | Yes | 100 TH/s |
| **FAILED** | Permanent failure after max backoffs | **No** | N/A |

---

## Signals

Signals are classified from tick data in priority order:

| Priority | Signal | Trigger Condition | Type |
|----------|--------|-------------------|------|
| 1 | `TIMER_EXPIRED` | WARMUP/BACKOFF timer done | Soft |
| 2 | `NO_JOBS` | `min_last_notify_age_s >= threshold` | **Hard** |
| 3 | `NO_DATA` | `delta_total < min_samples` | Soft |
| 4 | `HIGH_OTHER_REJECTS` | `ema_other_reject_rate >= threshold` | **Hard** |
| 5 | `HIGH_REJECTS` | `ema_reject_rate >= threshold` | Soft |
| 6 | `GOOD` | All thresholds passed | Soft |

**Hard signals** (NO_JOBS, HIGH_OTHER_REJECTS) always trigger BACKOFF from any state except FAILED.

---

## State Diagram

```
                    ┌─────────────────────────────────────────────────────┐
                    │                                                     │
                    ▼                                                     │
    ┌───────────────────────────────┐                                     │
    │           HEALTHY             │◄────────────────────────────────────┤
    │   (can receive hashrate)      │         3 good ticks                │
    └───────────────────────────────┘                                     │
         │              │                                                 │
         │ HIGH_REJECTS │ NO_JOBS/HIGH_OTHER_REJECTS                      │
         ▼              ▼                                                 │
    ┌──────────┐   ┌──────────┐      timer        ┌──────────┐           │
    │ DEGRADED │   │ BACKOFF  │─────expires──────►│PROBATION │───────────┘
    │(warning) │   │(excluded)│◄─────────────────┤│(capped)  │
    └──────────┘   └──────────┘  HIGH_REJECTS     └──────────┘
         │              │        or hard signal        │
         │ 3 good       │ level ≥ 5                    │ NO_JOBS/HIGH_OTHER
         │ ticks        ▼                              ▼
         │         ┌──────────┐                   ┌──────────┐
         └────────►│  FAILED  │◄──────────────────│ BACKOFF  │
           (via    │(terminal)│                   │(level++) │
          HEALTHY) └──────────┘                   └──────────┘

    ┌──────────┐
    │  WARMUP  │───timer expires───► HEALTHY
    │(new order)│
    └──────────┘
         │ NO_JOBS/HIGH_OTHER_REJECTS
         ▼
    ┌──────────┐
    │ BACKOFF  │
    └──────────┘
```

---

## Transition Tables

### HEALTHY

| Signal | Next State | Action |
|--------|------------|--------|
| `GOOD` | HEALTHY | — |
| `NO_DATA` | HEALTHY | — |
| `NO_JOBS` | **BACKOFF** | level++, set timer |
| `HIGH_REJECTS` | **DEGRADED** | reset good_tick_count |
| `HIGH_OTHER_REJECTS` | **BACKOFF** | level++, set timer |

### WARMUP

| Signal | Next State | Action |
|--------|------------|--------|
| `TIMER_EXPIRED` | **HEALTHY** | clear timer |
| `GOOD` | WARMUP | wait for timer |
| `NO_DATA` | WARMUP | wait for timer |
| `NO_JOBS` | **BACKOFF** | level++, set timer |
| `HIGH_REJECTS` | WARMUP | wait for timer |
| `HIGH_OTHER_REJECTS` | **BACKOFF** | level++, set timer |

### DEGRADED

| Signal | Next State | Action |
|--------|------------|--------|
| `GOOD` | DEGRADED or **HEALTHY** | good_tick_count++, if ≥3 → HEALTHY |
| `NO_DATA` | DEGRADED | counter preserved |
| `NO_JOBS` | **BACKOFF** | level++, set timer |
| `HIGH_REJECTS` | DEGRADED | reset good_tick_count |
| `HIGH_OTHER_REJECTS` | **BACKOFF** | level++, set timer |

### BACKOFF

| Signal | Next State | Action |
|--------|------------|--------|
| `TIMER_EXPIRED` | **PROBATION** | clear timer |
| `GOOD` | BACKOFF | wait for timer |
| `NO_DATA` | BACKOFF | wait for timer |
| `NO_JOBS` | BACKOFF (or **FAILED**) | level++, reset timer (FAILED if level≥5) |
| `HIGH_REJECTS` | BACKOFF | wait for timer |
| `HIGH_OTHER_REJECTS` | BACKOFF (or **FAILED**) | level++, reset timer (FAILED if level≥5) |

### PROBATION

| Signal | Next State | Action |
|--------|------------|--------|
| `GOOD` | PROBATION or **HEALTHY** | good_tick_count++, if ≥3 → HEALTHY (reset level) |
| `NO_DATA` | PROBATION | counter preserved, wait for miners |
| `NO_JOBS` | **BACKOFF** | level++, set timer |
| `HIGH_REJECTS` | **BACKOFF** | level++, set timer |
| `HIGH_OTHER_REJECTS` | **BACKOFF** | level++, set timer |

### FAILED

| Signal | Next State | Action |
|--------|------------|--------|
| `*` (any) | FAILED | — (terminal state) |

---

## Quick Reference Matrix

| From State | GOOD | NO_DATA | NO_JOBS | HIGH_REJECTS | HIGH_OTHER | TIMER |
|------------|------|---------|---------|--------------|------------|-------|
| **HEALTHY** | — | — | →BACKOFF | →DEGRADED | →BACKOFF | N/A |
| **WARMUP** | wait | wait | →BACKOFF | wait | →BACKOFF | →HEALTHY |
| **DEGRADED** | count++ | — | →BACKOFF | reset | →BACKOFF | N/A |
| **BACKOFF** | wait | wait | level++ | wait | level++ | →PROBATION |
| **PROBATION** | count++ | — | →BACKOFF | →BACKOFF | →BACKOFF | N/A |
| **FAILED** | — | — | — | — | — | — |

**Legend:**
- `—` = stay in current state
- `wait` = stay, waiting for timer
- `count++` = increment good_tick_count (→HEALTHY if ≥3)
- `reset` = reset good_tick_count to 0
- `level++` = increment backoff_level (→FAILED if ≥5)

---

## Recovery Paths

| From State | Recovery Path |
|------------|---------------|
| WARMUP | Timer expires → HEALTHY |
| DEGRADED | 3 consecutive GOOD signals → HEALTHY |
| BACKOFF | Timer expires → PROBATION → (3 GOOD) → HEALTHY |
| PROBATION | 3 consecutive GOOD signals → HEALTHY (resets backoff_level to 0) |
| FAILED | **No recovery** (terminal, order must be recreated) |

---

## Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `MAX_BACKOFF_LEVEL` | 5 | After 5 backoffs, order transitions to FAILED |
| `RECOVERY_TICKS_REQUIRED` | 3 | Consecutive good ticks needed to recover |

---

## Key Invariants

1. **Hard signals always win** — `NO_JOBS` and `HIGH_OTHER_REJECTS` trigger BACKOFF from any state (except FAILED)

2. **Timers can't get stuck** — Invalid timestamps (≤0) are treated as expired, ensuring orders can always recover

3. **good_tick_count preserved on NO_DATA** — Intermittent GOOD signals accumulate across NO_DATA gaps

4. **PROBATION is safe on NO_DATA** — Prevents infinite BACKOFF↔PROBATION loop when no miners are assigned yet

5. **FAILED is terminal** — No escape by design (after 5+ failures, order is permanently excluded)

---

## EMA Smoothing

Reject rates use Exponential Moving Average (EMA) smoothing to prevent false positives from single-tick spikes:

```
EMA_new = (1 - alpha) * EMA_prev + alpha * sample
```

The EMA values are updated **before** signal classification, ensuring hysteresis in state transitions.

---

## Code Locations

| Function | Location | Description |
|----------|----------|-------------|
| `classify_signal()` | pool_health.py:406-492 | Classifies tick data into signals |
| `compute_transition()` | pool_health.py:495-625 | Computes state transitions |
| `evaluate_pool_health()` | pool_health.py:804-913 | Main evaluation loop |
| `PoolHealthRule` | rules.py:182-264 | Excludes BACKOFF/FAILED orders |
| `PoolPostBackoffProbationCapRule` | rules.py:267-327 | Caps PROBATION orders to 100 TH/s |
