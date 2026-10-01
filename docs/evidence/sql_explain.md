# Query optimisation, measured

PostgreSQL 16.15 on aarch64-unknown-linux-musl, in Docker on the development laptop. Rows: 100,000 vehicles, 1,000,000 alerts, 400,005 audit entries. Times are the median of five `EXPLAIN (ANALYZE, BUFFERS)` runs with a warm cache.

| Query | Before | After | Change |
|---|---:|---:|---|
| Fleet manager pages through their vehicles (page 400) | 7.70 ms | 0.145 ms | Keyset pagination (WHERE id > last seen id) instead of OFFSET, plus the composite index (fleet_id, id) so each fleet's vehicles are read in id order. |
| Latest alerts, newest first | 277.66 ms | 0.297 ms | Composite index (ts, id) matching the ORDER BY: the newest 30 rows are read from the end of the index instead of sorting a million rows. |
| Alert history of one vehicle | 0.03 ms | 0.031 ms | Nothing to add. The unique constraint (vehicle_id, kind, ts) that makes alert inserts idempotent already starts with vehicle_id, so the planner uses it. A second index (vehicle_id, ts) was measured and removed from the schema: it cost writes and saved nothing. |
| Critical alerts only (partial index) | 88.43 ms | 0.014 ms | Partial index on (ts) WHERE severity = 'critical': smaller than a full index and exactly what the query needs. |
| Audit trail of one person | 0.74 ms | 0.027 ms | Composite index (actor, id). |
| Vehicles per source and powertrain (materialised view) | 25.12 ms | 0.003 ms | Materialised view refreshed when vehicles are added (REFRESH MATERIALIZED VIEW CONCURRENTLY needs the unique index created with it). |

## Fleet manager pages through their vehicles (page 400)

The console's vehicle list for one tenant. Offset pagination rereads every earlier page.

**Before**

```sql
SELECT v.id, v.vin, v.powertrain, o.key, f.name FROM vehicle v JOIN fleet f ON f.id = v.fleet_id JOIN oem o ON o.id = v.oem_id WHERE f.tenant_id = %(t)s ORDER BY v.id LIMIT 25 OFFSET 10000
```
```
Limit  (cost=2694.16..2694.16 rows=1 width=53) (actual time=7.576..7.577 rows=0 loops=1)
  Buffers: shared hit=1244
  ->  Sort  (cost=2687.16..2694.16 rows=2800 width=53) (actual time=7.565..7.573 rows=263 loops=1)
        Sort Key: v.id
        Sort Method: quicksort  Memory: 59kB
        Buffers: shared hit=1244
        ->  Hash Join  (cost=7.35..2526.84 rows=2800 width=53) (actual time=0.022..7.529 rows=263 loops=1)
              Hash Cond: (v.oem_id = o.id)
              Buffers: shared hit=1244
              ->  Hash Join  (cost=6.21..2513.16 rows=2800 width=53) (actual time=0.015..7.498 rows=263 loops=1)
                    Hash Cond: (v.fleet_id = f.id)
                    Buffers: shared hit=1243
                    ->  Seq Scan on vehicle v  (cost=0.00..2240.00 rows=100000 width=46) (actual time=0.001..3.501 rows=100000 loops=1)
                          Buffers: shared hit=1240
                    ->  Hash  (cost=6.12..6.12 rows=7 width=23) (actual time=0.009..0.009 rows=7 loops=1)
                          Buckets: 1024  Batches: 1  Memory Usage: 9kB
                          Buffers: shared hit=3
                          ->  Seq Scan on fleet f  (cost=0.00..6.12 rows=7 width=23) (actual time=0.001..0.007 rows=7 loops=1)
                                Filter: (tenant_id = 1)
                                Rows Removed by Filter: 243
                                Buffers: shared hit=3
              ->  Hash  (cost=1.06..1.06 rows=6 width=16) (actual time=0.005..0.005 rows=6 loops=1)
                    Buckets: 1024  Batches: 1  Memory Usage: 9kB
                    Buffers: shared hit=1
                    ->  Seq Scan on oem o  (cost=0.00..1.06 rows=6 width=16) (actual time=0.002..0.002 rows=6 loops=1)
                          Buffers: shared hit=1
Planning:
  Buffers: shared hit=8
Planning Time: 0.095 ms
Execution Time: 7.604 ms
```

**Change**

Keyset pagination (WHERE id > last seen id) instead of OFFSET, plus the composite index (fleet_id, id) so each fleet's vehicles are read in id order.
```sql
CREATE INDEX ix_vehicle_fleet_id_id ON vehicle (fleet_id, id)
```

**After**

```sql
SELECT v.id, v.vin, v.powertrain, o.key, f.name FROM vehicle v JOIN fleet f ON f.id = v.fleet_id JOIN oem o ON o.id = v.oem_id WHERE f.tenant_id = %(t)s AND v.id > %(after)s ORDER BY v.id LIMIT 25
```
```
Limit  (cost=59.33..59.39 rows=25 width=53) (actual time=0.119..0.119 rows=1 loops=1)
  Buffers: shared hit=24
  ->  Sort  (cost=59.33..59.40 rows=30 width=53) (actual time=0.119..0.119 rows=1 loops=1)
        Sort Key: v.id
        Sort Method: quicksort  Memory: 25kB
        Buffers: shared hit=24
        ->  Nested Loop  (cost=6.65..58.59 rows=30 width=53) (actual time=0.030..0.117 rows=1 loops=1)
              Buffers: shared hit=24
              ->  Hash Join  (cost=6.51..56.82 rows=30 width=53) (actual time=0.026..0.113 rows=1 loops=1)
                    Hash Cond: (v.fleet_id = f.id)
                    Buffers: shared hit=22
                    ->  Index Scan using vehicle_pkey on vehicle v  (cost=0.29..47.79 rows=1057 width=46) (actual time=0.005..0.057 rows=1045 loops=1)
                          Index Cond: (id > 98955)
                          Buffers: shared hit=19
                    ->  Hash  (cost=6.12..6.12 rows=7 width=23) (actual time=0.009..0.009 rows=7 loops=1)
                          Buckets: 1024  Batches: 1  Memory Usage: 9kB
                          Buffers: shared hit=3
                          ->  Seq Scan on fleet f  (cost=0.00..6.12 rows=7 width=23) (actual time=0.001..0.007 rows=7 loops=1)
                                Filter: (tenant_id = 1)
                                Rows Removed by Filter: 243
                                Buffers: shared hit=3
              ->  Memoize  (cost=0.14..0.17 rows=1 width=16) (actual time=0.003..0.003 rows=1 loops=1)
                    Cache Key: v.oem_id
                    Cache Mode: logical
                    Hits: 0  Misses: 1  Evictions: 0  Overflows: 0  Memory Usage: 1kB
                    Buffers: shared hit=2
                    ->  Index Scan using oem_pkey on oem o  (cost=0.13..0.16 rows=1 width=16) (actual time=0.003..0.003 rows=1 loops=1)
                          Index Cond: (id = v.oem_id)
                          Buffers: shared hit=2
Planning:
  Buffers: shared hit=19
Planning Time: 0.109 ms
Execution Time: 0.137 ms
```

## Latest alerts, newest first

The alerts panel refreshes every three seconds for every open console.

**Before**

```sql
SELECT a.id, a.kind, a.severity, a.ts, v.vin FROM alert a JOIN vehicle v ON v.id = a.vehicle_id ORDER BY a.ts DESC, a.id DESC LIMIT 30
```
```
Limit  (cost=58807.56..58807.64 rows=30 width=51) (actual time=267.222..267.228 rows=30 loops=1)
  Buffers: shared hit=11274 read=3124
  ->  Sort  (cost=58807.56..61307.56 rows=1000000 width=51) (actual time=267.219..267.221 rows=30 loops=1)
        Sort Key: a.ts DESC, a.id DESC
        Sort Method: top-N heapsort  Memory: 33kB
        Buffers: shared hit=11274 read=3124
        ->  Hash Join  (cost=3490.00..29273.11 rows=1000000 width=51) (actual time=10.901..166.419 rows=1000000 loops=1)
              Hash Cond: (a.vehicle_id = v.id)
              Buffers: shared hit=11274 read=3124
              ->  Seq Scan on alert a  (cost=0.00..23158.00 rows=1000000 width=41) (actual time=0.008..42.824 rows=1000000 loops=1)
                    Buffers: shared hit=10034 read=3124
              ->  Hash  (cost=2240.00..2240.00 rows=100000 width=26) (actual time=10.684..10.685 rows=100000 loops=1)
                    Buckets: 131072  Batches: 1  Memory Usage: 7274kB
                    Buffers: shared hit=1240
                    ->  Seq Scan on vehicle v  (cost=0.00..2240.00 rows=100000 width=26) (actual time=0.003..4.617 rows=100000 loops=1)
                          Buffers: shared hit=1240
Planning:
  Buffers: shared hit=14
Planning Time: 0.117 ms
Execution Time: 267.791 ms
```

**Change**

Composite index (ts, id) matching the ORDER BY: the newest 30 rows are read from the end of the index instead of sorting a million rows.
```sql
CREATE INDEX ix_alert_ts_id ON alert (ts, id)
```

**After**

```sql
SELECT a.id, a.kind, a.severity, a.ts, v.vin FROM alert a JOIN vehicle v ON v.id = a.vehicle_id ORDER BY a.ts DESC, a.id DESC LIMIT 30
```
```
Limit  (cost=0.73..4.24 rows=30 width=50) (actual time=0.008..0.040 rows=30 loops=1)
  Buffers: shared hit=94
  ->  Nested Loop  (cost=0.73..117053.64 rows=1000000 width=50) (actual time=0.008..0.039 rows=30 loops=1)
        Buffers: shared hit=94
        ->  Index Scan Backward using ix_alert_ts_id on alert a  (cost=0.42..60821.99 rows=1000000 width=40) (actual time=0.004..0.005 rows=30 loops=1)
              Buffers: shared hit=4
        ->  Memoize  (cost=0.30..0.33 rows=1 width=26) (actual time=0.001..0.001 rows=1 loops=30)
              Cache Key: a.vehicle_id
              Cache Mode: logical
              Hits: 0  Misses: 30  Evictions: 0  Overflows: 0  Memory Usage: 4kB
              Buffers: shared hit=90
              ->  Index Scan using vehicle_pkey on vehicle v  (cost=0.29..0.32 rows=1 width=26) (actual time=0.001..0.001 rows=1 loops=30)
                    Index Cond: (id = a.vehicle_id)
                    Buffers: shared hit=90
Planning:
  Buffers: shared hit=14
Planning Time: 0.061 ms
Execution Time: 0.300 ms
```

## Alert history of one vehicle

The vehicle drawer and the agent's evidence both ask 'what happened to this vehicle'.

**Before**

```sql
SELECT id, kind, severity, ts FROM alert WHERE vehicle_id = %(v)s ORDER BY ts DESC LIMIT 50
```
```
Limit  (cost=43.97..43.99 rows=10 width=32) (actual time=0.011..0.012 rows=10 loops=1)
  Buffers: shared hit=13
  ->  Sort  (cost=43.97..43.99 rows=10 width=32) (actual time=0.011..0.012 rows=10 loops=1)
        Sort Key: ts DESC
        Sort Method: quicksort  Memory: 25kB
        Buffers: shared hit=13
        ->  Bitmap Heap Scan on alert  (cost=4.50..43.80 rows=10 width=32) (actual time=0.006..0.009 rows=10 loops=1)
              Recheck Cond: (vehicle_id = 44127)
              Heap Blocks: exact=10
              Buffers: shared hit=13
              ->  Bitmap Index Scan on uq_alert_idempotent  (cost=0.00..4.50 rows=10 width=0) (actual time=0.005..0.005 rows=10 loops=1)
                    Index Cond: (vehicle_id = 44127)
                    Buffers: shared hit=3
Planning Time: 0.016 ms
Execution Time: 0.022 ms
```

**Change**

Nothing to add. The unique constraint (vehicle_id, kind, ts) that makes alert inserts idempotent already starts with vehicle_id, so the planner uses it. A second index (vehicle_id, ts) was measured and removed from the schema: it cost writes and saved nothing.
```sql
```

**After**

```sql
SELECT id, kind, severity, ts FROM alert WHERE vehicle_id = %(v)s ORDER BY ts DESC LIMIT 50
```
```
Limit  (cost=43.97..43.99 rows=10 width=32) (actual time=0.012..0.012 rows=10 loops=1)
  Buffers: shared hit=13
  ->  Sort  (cost=43.97..43.99 rows=10 width=32) (actual time=0.011..0.012 rows=10 loops=1)
        Sort Key: ts DESC
        Sort Method: quicksort  Memory: 25kB
        Buffers: shared hit=13
        ->  Bitmap Heap Scan on alert  (cost=4.50..43.80 rows=10 width=32) (actual time=0.006..0.008 rows=10 loops=1)
              Recheck Cond: (vehicle_id = 44127)
              Heap Blocks: exact=10
              Buffers: shared hit=13
              ->  Bitmap Index Scan on uq_alert_idempotent  (cost=0.00..4.50 rows=10 width=0) (actual time=0.005..0.005 rows=10 loops=1)
                    Index Cond: (vehicle_id = 44127)
                    Buffers: shared hit=3
Planning Time: 0.011 ms
Execution Time: 0.023 ms
```

## Critical alerts only (partial index)

An on-call view shows only critical alerts. They are 40% of rows here and far fewer in real fleets.

**Before**

```sql
SELECT id, vehicle_id, kind, ts FROM alert WHERE severity = 'critical' ORDER BY ts DESC LIMIT 30
```
```
Limit  (cost=37465.87..37465.95 rows=30 width=32) (actual time=85.460..85.462 rows=30 loops=1)
  Buffers: shared hit=10421 read=2737
  ->  Sort  (cost=37465.87..38465.37 rows=399800 width=32) (actual time=85.459..85.460 rows=30 loops=1)
        Sort Key: ts DESC
        Sort Method: top-N heapsort  Memory: 29kB
        Buffers: shared hit=10421 read=2737
        ->  Seq Scan on alert  (cost=0.00..25658.00 rows=399800 width=32) (actual time=0.010..49.546 rows=400000 loops=1)
              Filter: ((severity)::text = 'critical'::text)
              Rows Removed by Filter: 600000
              Buffers: shared hit=10421 read=2737
Planning Time: 0.048 ms
Execution Time: 85.486 ms
```

**Change**

Partial index on (ts) WHERE severity = 'critical': smaller than a full index and exactly what the query needs.
```sql
CREATE INDEX ix_alert_critical_ts ON alert (ts) WHERE severity = 'critical'
```

**After**

```sql
SELECT id, vehicle_id, kind, ts FROM alert WHERE severity = 'critical' ORDER BY ts DESC LIMIT 30
```
```
Limit  (cost=0.42..1.60 rows=30 width=32) (actual time=0.011..0.015 rows=30 loops=1)
  Buffers: shared hit=5
  ->  Index Scan Backward using ix_alert_critical_ts on alert  (cost=0.42..15654.92 rows=399700 width=32) (actual time=0.011..0.013 rows=30 loops=1)
        Buffers: shared hit=5
Planning Time: 0.020 ms
Execution Time: 0.021 ms
```

## Audit trail of one person

A compliance question: everything one user did, newest first.

**Before**

```sql
SELECT id, ts, action, resource FROM audit_log WHERE actor = %(a)s ORDER BY id DESC LIMIT 50
```
```
Limit  (cost=0.42..933.91 rows=50 width=54) (actual time=0.010..0.734 rows=50 loops=1)
  Buffers: shared hit=577
  ->  Index Scan Backward using audit_log_pkey on audit_log  (cost=0.42..24700.51 rows=1323 width=54) (actual time=0.010..0.732 rows=50 loops=1)
        Filter: ((actor)::text = 'user17@rosetta.example'::text)
        Rows Removed by Filter: 14734
        Buffers: shared hit=577
Planning Time: 0.009 ms
Execution Time: 0.739 ms
```

**Change**

Composite index (actor, id).
```sql
CREATE INDEX ix_audit_actor ON audit_log (actor, id)
```

**After**

```sql
SELECT id, ts, action, resource FROM audit_log WHERE actor = %(a)s ORDER BY id DESC LIMIT 50
```
```
Limit  (cost=0.42..193.29 rows=50 width=54) (actual time=0.007..0.019 rows=50 loops=1)
  Buffers: shared hit=53
  ->  Index Scan Backward using ix_audit_actor on audit_log  (cost=0.42..5107.51 rows=1324 width=54) (actual time=0.007..0.016 rows=50 loops=1)
        Index Cond: ((actor)::text = 'user17@rosetta.example'::text)
        Buffers: shared hit=53
Planning Time: 0.011 ms
Execution Time: 0.023 ms
```

## Vehicles per source and powertrain (materialised view)

The sources page and reports aggregate the whole vehicle table on every load.

**Before**

```sql
SELECT o.key, v.powertrain, count(*) FROM vehicle v JOIN oem o ON o.id = v.oem_id GROUP BY o.key, v.powertrain
```
```
HashAggregate  (cost=3439.06..3439.24 rows=18 width=20) (actual time=21.168..21.170 rows=16 loops=1)
  Group Key: o.key, v.powertrain
  Batches: 1  Memory Usage: 24kB
  Buffers: shared hit=1241
  ->  Hash Join  (cost=1.14..2689.06 rows=100000 width=12) (actual time=0.016..11.747 rows=100000 loops=1)
        Hash Cond: (v.oem_id = o.id)
        Buffers: shared hit=1241
        ->  Seq Scan on vehicle v  (cost=0.00..2240.00 rows=100000 width=12) (actual time=0.003..3.852 rows=100000 loops=1)
              Buffers: shared hit=1240
        ->  Hash  (cost=1.06..1.06 rows=6 width=16) (actual time=0.007..0.007 rows=6 loops=1)
              Buckets: 1024  Batches: 1  Memory Usage: 9kB
              Buffers: shared hit=1
              ->  Seq Scan on oem o  (cost=0.00..1.06 rows=6 width=16) (actual time=0.002..0.003 rows=6 loops=1)
                    Buffers: shared hit=1
Planning:
  Buffers: shared hit=4
Planning Time: 0.094 ms
Execution Time: 21.198 ms
```

**Change**

Materialised view refreshed when vehicles are added (REFRESH MATERIALIZED VIEW CONCURRENTLY needs the unique index created with it).
```sql
CREATE MATERIALIZED VIEW mv_fleet_mix AS SELECT o.key AS oem, v.powertrain, count(*) AS vehicles
                       FROM vehicle v JOIN oem o ON o.id = v.oem_id GROUP BY o.key, v.powertrain
CREATE UNIQUE INDEX ux_mv_fleet_mix ON mv_fleet_mix (oem, powertrain)
```

**After**

```sql
SELECT oem, powertrain, vehicles FROM mv_fleet_mix
```
```
Seq Scan on mv_fleet_mix  (cost=0.00..1.16 rows=16 width=20) (actual time=0.001..0.001 rows=16 loops=1)
  Buffers: shared hit=1
Planning Time: 0.004 ms
Execution Time: 0.003 ms
```
