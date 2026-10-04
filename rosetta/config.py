"""Configuration from environment variables (12-factor: config lives in the environment).

Nothing here is cloud specific. The same image runs locally, on AWS, GCP or
Azure: only these variables change.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def kafka_client_config() -> dict[str, str]:
    """Extra librdkafka settings from ROSETTA_KAFKA_CFG_* variables.

    ROSETTA_KAFKA_CFG_SECURITY_PROTOCOL=SASL_SSL becomes {"security.protocol": "SASL_SSL"}.
    This is how the same image talks to MSK (TLS, IAM), Confluent Cloud or Event Hubs
    (SASL_SSL) or Aiven (mTLS) without a code change.
    """
    prefix = "ROSETTA_KAFKA_CFG_"
    return {k[len(prefix):].lower().replace("_", "."): v for k, v in os.environ.items() if k.startswith(prefix)}


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    env: str = field(default_factory=lambda: _env("ROSETTA_ENV", "local"))
    data_dir: Path = field(default_factory=lambda: Path(_env("ROSETTA_DATA_DIR", "./data")).resolve())

    # stores: each has a local adapter and a production adapter
    database_url: str = field(default_factory=lambda: _env("ROSETTA_DATABASE_URL", ""))
    broker: str = field(default_factory=lambda: _env("ROSETTA_BROKER", "log"))            # log | kafka
    kafka_bootstrap: str = field(default_factory=lambda: _env("ROSETTA_KAFKA_BOOTSTRAP", "localhost:9092"))
    kafka_replication: int = field(default_factory=lambda: _int("ROSETTA_KAFKA_REPLICATION", 1))
    state_store: str = field(default_factory=lambda: _env("ROSETTA_STATE_STORE", "memory"))  # memory | redis
    redis_url: str = field(default_factory=lambda: _env("ROSETTA_REDIS_URL", "redis://localhost:6379/0"))
    archive: str = field(default_factory=lambda: _env("ROSETTA_ARCHIVE", "fs"))            # fs | s3
    s3_endpoint: str = field(default_factory=lambda: _env("ROSETTA_S3_ENDPOINT", ""))
    s3_bucket: str = field(default_factory=lambda: _env("ROSETTA_S3_BUCKET", "rosetta-telemetry"))
    s3_access_key: str = field(default_factory=lambda: _env("ROSETTA_S3_ACCESS_KEY", ""))
    s3_secret_key: str = field(default_factory=lambda: _env("ROSETTA_S3_SECRET_KEY", ""))

    raw_partitions: int = field(default_factory=lambda: _int("ROSETTA_RAW_PARTITIONS", 8))
    log_retention_mb: int = field(default_factory=lambda: _int("ROSETTA_LOG_RETENTION_MB", 160))
    # Dead letters must outlive the time it takes to write a mapping, so they get
    # their own, larger budget. In Kafka this is a retention.ms of days.
    dlq_retention_mb: int = field(default_factory=lambda: _int("ROSETTA_DLQ_RETENTION_MB", 2048))
    telemetry_store: str = field(default_factory=lambda: _env("ROSETTA_TELEMETRY_STORE", "parquet"))  # parquet | timescale
    mqtt_url: str = field(default_factory=lambda: _env("ROSETTA_MQTT_URL", "mqtt://localhost:1883"))
    mqtt_ca: str = field(default_factory=lambda: _env("ROSETTA_MQTT_CA", ""))
    mqtt_cert: str = field(default_factory=lambda: _env("ROSETTA_MQTT_CERT", ""))
    mqtt_key: str = field(default_factory=lambda: _env("ROSETTA_MQTT_KEY", ""))
    archive_retention_mb: int = field(default_factory=lambda: _int("ROSETTA_ARCHIVE_RETENTION_MB", 300))

    # security
    jwt_secret: str = field(default_factory=lambda: _env("ROSETTA_JWT_SECRET", ""))
    jwt_issuer: str = field(default_factory=lambda: _env("ROSETTA_JWT_ISSUER", "rosetta"))
    jwt_ttl_s: int = field(default_factory=lambda: _int("ROSETTA_JWT_TTL_S", 3600))
    cors_origins: str = field(default_factory=lambda: _env("ROSETTA_CORS_ORIGINS", "http://localhost:5173"))
    rate_limit_per_min: int = field(default_factory=lambda: _int("ROSETTA_RATE_LIMIT_PER_MIN", 600))
    oidc_jwks_url: str = field(default_factory=lambda: _env("ROSETTA_OIDC_JWKS_URL", ""))
    oidc_audience: str = field(default_factory=lambda: _env("ROSETTA_OIDC_AUDIENCE", "rosetta-api"))
    embed_pipeline: bool = field(default_factory=lambda: _bool("ROSETTA_EMBED_PIPELINE", False))
    normalizers: int = field(default_factory=lambda: _int("ROSETTA_NORMALIZERS", 4))
    processors: int = field(default_factory=lambda: _int("ROSETTA_PROCESSORS", 2))
    sim_shards: int = field(default_factory=lambda: _int("ROSETTA_SIM_SHARDS", 1))
    vehicles: int = field(default_factory=lambda: _int("ROSETTA_VEHICLES", 100_000))
    web_dist: str = field(default_factory=lambda: _env("ROSETTA_WEB_DIST", "./web/dist"))
    mask_precision: int = field(default_factory=lambda: _int("ROSETTA_MASK_GEOHASH_PRECISION", 5))

    # agent
    # auto picks whichever provider has credentials. The model defaults to that
    # provider's own default (rosetta/agent/llm_agent.py), so it is empty here.
    llm_provider: str = field(default_factory=lambda: _env("ROSETTA_LLM_PROVIDER", "auto"))  # auto | anthropic | google | none
    llm_model: str = field(default_factory=lambda: _env("ROSETTA_LLM_MODEL", ""))
    golden_pass_rate: float = field(default_factory=lambda: float(_env("ROSETTA_GOLDEN_PASS_RATE", "0.99")))

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{self.data_dir / 'rosetta.db'}"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "log"

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive"

    @property
    def is_sqlite(self) -> bool:
        return self.db_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    get_settings.cache_clear()
