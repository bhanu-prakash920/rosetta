-- Rosetta: what PostgreSQL needs beyond the relational schema.
--
--   1. extensions            timescaledb, vector (pgvector)
--   2. telemetry             the time-series table behind ROSETTA_TELEMETRY_STORE=timescale
--   3. embedding.vec         the pgvector column and HNSW index behind the agent's mapping memory
--
-- ORDER OF OPERATIONS (this is the order docker-compose.yml and the Helm chart use)
--
--   step 1  migrate   the application creates the relational tables
--                     (SQLAlchemy create_all, rosetta/db/models.py)
--   step 2  this file
--   step 3  seed      python -m rosetta seed
--
-- Why this file cannot simply run last: on PostgreSQL the seed job writes the
-- mapping memory through PgVectorMemory (rosetta/agent/memory.py), which runs
-- `UPDATE embedding SET vec = ...`. The column `vec` is created here, and it can
-- only be added once the table `embedding` exists. So the schema comes first,
-- then this file, then the seed.
--
-- Why it is still safe to run at any time: every statement is idempotent, and the
-- part that touches `embedding` is skipped with a notice when that table does not
-- exist yet. Running the file before step 1 creates the extensions and the
-- telemetry table and nothing else. Run it again after step 1 to add the column.
--
-- TimescaleDB is optional. Where the extension cannot be installed (Amazon RDS
-- for PostgreSQL does not offer it), `telemetry` is created as a plain table
-- with the same columns and the same unique index, so the application works
-- unchanged. The hypertable, compression, retention and the continuous
-- aggregate are skipped, and a notice says so.
--
-- HOW TO RUN. With psql, because the file uses psql conditionals (\if and :{?name}, psql 11 or newer):
--
--   psql "postgresql://USER@HOST:5432/rosetta" -v ON_ERROR_STOP=1 -f infra/sql/postgres_extras.sql
--
-- Optional settings, passed with -v:
--   retention_days    default 90    telemetry older than this is dropped
--   compress_days     default 7     chunks older than this are compressed
--   vin_partitions    default 8     hash partitions on vin (space dimension)
--
-- The role needs the right to create extensions (superuser, or rds_superuser on RDS).

\set ON_ERROR_STOP on

\if :{?retention_days}
\else
  \set retention_days 90
\endif
\if :{?compress_days}
\else
  \set compress_days 7
\endif
\if :{?vin_partitions}
\else
  \set vin_partitions 8
\endif

-- --------------------------------------------------------------- 1. extensions
SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb') AS has_timescale \gset
SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector') AS has_vector \gset

\if :has_timescale
  CREATE EXTENSION IF NOT EXISTS timescaledb;
\else
  \echo 'NOTICE: extension timescaledb is not available on this server. telemetry will be a plain table.'
\endif

\if :has_vector
  CREATE EXTENSION IF NOT EXISTS vector;
\else
  \echo 'NOTICE: extension vector (pgvector) is not available on this server. The mapping memory cannot use PostgreSQL.'
\endif

-- ---------------------------------------------------------------- 2. telemetry
-- Columns and their order match COLUMNS in rosetta/adapters/timescale.py:
--   ts, vin, seq, oem, map_v, lat, lon, speed_kmh, heading_deg, odo_km, soc_pct,
--   fuel_pct, ambient_c, ignition, evt, dtc, rx_ts, geohash5
-- Types follow the Arrow schema of the archive (rosetta/adapters/archive.py):
-- float32 -> real, float64 -> double precision, int64 -> bigint, int32 -> integer.
-- ts is the event time. rx_ts stays what the pipeline carries: epoch milliseconds.
CREATE TABLE IF NOT EXISTS telemetry (
    ts          timestamptz       NOT NULL,
    vin         text              NOT NULL,
    seq         bigint            NOT NULL,
    oem         text,
    map_v       integer,
    lat         double precision,
    lon         double precision,
    speed_kmh   real,
    heading_deg real,
    odo_km      double precision,
    soc_pct     real,
    fuel_pct    real,
    ambient_c   real,
    ignition    boolean,
    evt         text,
    dtc         text[],
    rx_ts       bigint,
    geohash5    text
);

\if :has_timescale
  -- Partitioned by time (one chunk per day) and by hash of vin (space dimension).
  SELECT create_hypertable('telemetry', 'ts',
                           partitioning_column => 'vin',
                           number_partitions   => :vin_partitions,
                           chunk_time_interval => INTERVAL '1 day',
                           if_not_exists       => TRUE);
\endif

-- The sink inserts with ON CONFLICT (vin, ts, seq) DO NOTHING, which needs exactly
-- this unique index. On a hypertable a unique index must contain every
-- partitioning column: ts and vin are both in it.
CREATE UNIQUE INDEX IF NOT EXISTS telemetry_vin_ts_seq_uq ON telemetry (vin, ts, seq);

-- "Latest events of one source" and the continuous aggregate read by oem and time.
CREATE INDEX IF NOT EXISTS telemetry_oem_ts_idx ON telemetry (oem, ts DESC);

\if :has_timescale
  -- Compression: one segment per vehicle, ordered by time, so the history of one
  -- vehicle decompresses a single segment.
  -- Set once: some TimescaleDB versions refuse to change these settings after
  -- chunks have been compressed, which would break a later re-run of this file.
  SELECT NOT COALESCE((SELECT compression_enabled FROM timescaledb_information.hypertables
                       WHERE hypertable_schema = 'public' AND hypertable_name = 'telemetry'), FALSE)
         AS needs_compression \gset
  \if :needs_compression
    ALTER TABLE telemetry SET (
        timescaledb.compress,
        timescaledb.compress_segmentby = 'vin',
        timescaledb.compress_orderby   = 'ts DESC, seq DESC'
    );
  \endif
  SELECT add_compression_policy('telemetry', compress_after => make_interval(days => :compress_days),
                                if_not_exists => TRUE);

  -- Retention: the Parquet archive in object storage is the long-term record.
  SELECT add_retention_policy('telemetry', drop_after => make_interval(days => :retention_days),
                              if_not_exists => TRUE);

  -- Continuous aggregate: events and average speed per source per minute.
  CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_oem_minute
  WITH (timescaledb.continuous) AS
  SELECT time_bucket(INTERVAL '1 minute', ts) AS bucket,
         oem,
         count(*)       AS events,
         avg(speed_kmh) AS avg_speed_kmh
  FROM telemetry
  GROUP BY bucket, oem
  WITH NO DATA;

  SELECT add_continuous_aggregate_policy('telemetry_oem_minute',
                                         start_offset      => INTERVAL '2 hours',
                                         end_offset        => INTERVAL '1 minute',
                                         schedule_interval => INTERVAL '1 minute',
                                         if_not_exists     => TRUE);
\endif

-- ------------------------------------------------------------ 3. embedding.vec
-- vector(304): 304 is DIM in rosetta/agent/memory.py, which is
-- N_DENSE (48) + NAME_DIM (256) from rosetta/ml/profile.py. Check it with
--   python -c "from rosetta.agent.memory import DIM; print(DIM)"
-- If DIM changes, this column has to be recreated with the new size.
SELECT to_regclass('public.embedding') IS NOT NULL AS has_embedding \gset

\if :has_vector
  \if :has_embedding
    ALTER TABLE embedding ADD COLUMN IF NOT EXISTS vec vector(304);
    -- Cosine distance, because PgVectorMemory.search orders by `vec <=> query`.
    CREATE INDEX IF NOT EXISTS embedding_vec_hnsw ON embedding
        USING hnsw (vec vector_cosine_ops) WITH (m = 16, ef_construction = 64);
  \else
    \echo 'NOTICE: table embedding does not exist yet. Create the schema (step 1) and run this file again.'
  \endif
\endif
