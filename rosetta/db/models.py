"""Relational core, in Third Normal Form.

Everything here needs transactions and strong consistency: who owns what, who
may see what, which mapping is live, and the audit trail. Telemetry itself is
not stored here (see docs/adr): it goes to the time-series store and Parquet.

Deliberate denormalisations, each documented in the ER write-up:
  * mapping_version.spec  the full mapping as one JSON document next to the
    normalised mapping_field rows, so workers compile a version with one read.
  * metric_minute         a rollup table, rebuilt from the stream, for charts.
  * dlq_group.sample      one example payload kept with the group for the UI.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


# BigInteger primary keys do not autoincrement on SQLite, so use a variant.
PK = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    pass


# ------------------------------------------------------------ tenancy and access
class Tenant(Base):
    __tablename__ = "tenant"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[str] = mapped_column(String(24), default="fleet")  # fleet | lender | dealer | oem
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Subscription(Base):
    __tablename__ = "subscription"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"), index=True)
    plan: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="active")
    vehicle_quota: Mapped[int] = mapped_column(Integer)
    price_cents: Mapped[int] = mapped_column(Integer)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (CheckConstraint("price_cents >= 0"), CheckConstraint("vehicle_quota >= 0"))


class Role(Base):
    __tablename__ = "role"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    name: Mapped[str] = mapped_column(String(32), unique=True)
    description: Mapped[str] = mapped_column(String(200), default="")


class AppUser(Base):
    __tablename__ = "app_user"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"), nullable=True, index=True)
    email: Mapped[str] = mapped_column(String(200), unique=True)
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    roles: Mapped[list[Role]] = relationship(secondary="user_role", lazy="selectin")


class UserRole(Base):
    __tablename__ = "user_role"
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("role.id", ondelete="CASCADE"), primary_key=True)


# ------------------------------------------------------------------ fleet domain
class Fleet(Base):
    __tablename__ = "fleet"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    home_city: Mapped[str] = mapped_column(String(64), default="")
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)


class Oem(Base):
    __tablename__ = "oem"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    wmi: Mapped[str] = mapped_column(String(3))
    wire_format: Mapped[str] = mapped_column(String(24), default="json")
    status: Mapped[str] = mapped_column(String(16), default="onboarding")  # onboarding | live | paused
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Vehicle(Base):
    __tablename__ = "vehicle"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    vin: Mapped[str] = mapped_column(String(17), unique=True)
    device_id: Mapped[str] = mapped_column(String(32), unique=True)
    oem_id: Mapped[int] = mapped_column(ForeignKey("oem.id"), index=True)
    fleet_id: Mapped[int] = mapped_column(ForeignKey("fleet.id", ondelete="CASCADE"))
    powertrain: Mapped[str] = mapped_column(String(8))  # ICE | BEV | PHEV
    model_year: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        CheckConstraint("length(vin) = 17", name="ck_vehicle_vin_len"),
        Index("ix_vehicle_fleet_id_id", "fleet_id", "id"),  # keyset pagination inside a fleet
    )


class Driver(Base):
    """Personal data lives here and only here, so erasure has one place to act."""
    __tablename__ = "driver"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"), index=True)
    full_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    license_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VehicleDriverAssignment(Base):
    __tablename__ = "vehicle_driver_assignment"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicle.id", ondelete="CASCADE"), index=True)
    driver_id: Mapped[int] = mapped_column(ForeignKey("driver.id", ondelete="CASCADE"), index=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Trip(Base):
    __tablename__ = "trip"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicle.id", ondelete="CASCADE"))
    driver_id: Mapped[int | None] = mapped_column(ForeignKey("driver.id", ondelete="SET NULL"), nullable=True)
    start_ts: Mapped[int] = mapped_column(BigInteger)
    end_ts: Mapped[int] = mapped_column(BigInteger)
    distance_km: Mapped[float] = mapped_column(Float)
    max_speed_kmh: Mapped[float] = mapped_column(Float)
    start_geohash: Mapped[str] = mapped_column(String(12))
    end_geohash: Mapped[str] = mapped_column(String(12))
    __table_args__ = (
        UniqueConstraint("vehicle_id", "start_ts", name="uq_trip_vehicle_start"),
        CheckConstraint("end_ts >= start_ts"),
    )


class Alert(Base):
    __tablename__ = "alert"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicle.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(12))  # info | warning | critical
    ts: Mapped[int] = mapped_column(BigInteger)
    detected_ts: Mapped[int] = mapped_column(BigInteger)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    acknowledged_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True)
    __table_args__ = (
        UniqueConstraint("vehicle_id", "kind", "ts", name="uq_alert_idempotent"),
        Index("ix_alert_ts_id", "ts", "id"),
        # "critical alerts, newest first" for the on-call view. Measured in docs/evidence/sql_explain.md.
        Index("ix_alert_critical_ts", "ts", postgresql_where=text("severity = 'critical'"),
              sqlite_where=text("severity = 'critical'")),
    )


# ------------------------------------------------------------- mapping registry
class MappingVersion(Base):
    __tablename__ = "mapping_version"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    oem_id: Mapped[int] = mapped_column(ForeignKey("oem.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16), default="draft")
    canary_pct: Mapped[int] = mapped_column(Integer, default=0)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)
    spec_hash: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(16), default="human")  # seed | agent | human
    parent_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fields: Mapped[list[MappingField]] = relationship(cascade="all, delete-orphan", lazy="selectin")
    __table_args__ = (
        UniqueConstraint("oem_id", "version", name="uq_mapping_oem_version"),
        CheckConstraint("canary_pct BETWEEN 0 AND 100"),
        Index("ix_mapping_state", "state"),
    )


class MappingField(Base):
    __tablename__ = "mapping_field"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    mapping_version_id: Mapped[int] = mapped_column(ForeignKey("mapping_version.id", ondelete="CASCADE"), index=True)
    canonical_field: Mapped[str] = mapped_column(String(32))
    source_path: Mapped[str] = mapped_column(String(200))
    transforms: Mapped[list[Any]] = mapped_column(JSON, default=list)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (UniqueConstraint("mapping_version_id", "canonical_field"),)


class MappingAction(Base):
    """Every state change of a mapping: who did it, when, and why."""
    __tablename__ = "mapping_action"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    mapping_version_id: Mapped[int] = mapped_column(ForeignKey("mapping_version.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(16))
    from_state: Mapped[str] = mapped_column(String(16))
    to_state: Mapped[str] = mapped_column(String(16))
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True)
    actor_kind: Mapped[str] = mapped_column(String(12), default="user")  # user | agent | system
    comment: Mapped[str] = mapped_column(Text, default="")
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GoldenCase(Base):
    """A raw payload with the canonical event it must produce."""
    __tablename__ = "golden_case"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    oem_id: Mapped[int] = mapped_column(ForeignKey("oem.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(64), default="")
    payload: Mapped[bytes] = mapped_column(LargeBinary)
    expected: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ValidationRun(Base):
    __tablename__ = "validation_run"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    mapping_version_id: Mapped[int] = mapped_column(ForeignKey("mapping_version.id", ondelete="CASCADE"), index=True)
    total: Mapped[int] = mapped_column(Integer)
    passed: Mapped[int] = mapped_column(Integer)
    field_accuracy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    failures: Mapped[list[Any]] = mapped_column(JSON, default=list)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RegistryEpoch(Base):
    """One row. Bumped in the same transaction as any change to live mappings."""
    __tablename__ = "registry_epoch"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    epoch: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------- agent
class AgentRun(Base):
    __tablename__ = "agent_run"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    oem_id: Mapped[int] = mapped_column(ForeignKey("oem.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="running")
    engine: Mapped[str] = mapped_column(String(64), default="")
    trigger: Mapped[str] = mapped_column(String(24), default="manual")
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True)
    mapping_version_id: Mapped[int | None] = mapped_column(ForeignKey("mapping_version.id", ondelete="SET NULL"), nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    steps: Mapped[list[AgentStep]] = relationship(cascade="all, delete-orphan", order_by="AgentStep.seq", lazy="selectin")


class AgentStep(Base):
    __tablename__ = "agent_step"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_run.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    tool: Mapped[str] = mapped_column(String(48))
    input: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("run_id", "seq"),)


class Embedding(Base):
    """Vectors for retrieval. A real `vector` column with an HNSW index on PostgreSQL."""
    __tablename__ = "embedding"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    kind: Mapped[str] = mapped_column(String(24))          # field | mapping | incident
    ref: Mapped[str] = mapped_column(String(120))
    text: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    vector: Mapped[bytes] = mapped_column(LargeBinary)     # float32 little endian
    __table_args__ = (UniqueConstraint("kind", "ref"), Index("ix_embedding_kind", "kind"))


# -------------------------------------------------------- dead letters, replay
class DlqGroup(Base):
    __tablename__ = "dlq_group"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    oem_key: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(24))
    field: Mapped[str] = mapped_column(String(64), default="")
    shape: Mapped[str] = mapped_column(String(32), default="")
    count: Mapped[int] = mapped_column(BigInteger, default=0)
    replayed: Mapped[int] = mapped_column(BigInteger, default=0)
    first_seen: Mapped[int] = mapped_column(BigInteger)
    last_seen: Mapped[int] = mapped_column(BigInteger)
    detail: Mapped[str] = mapped_column(String(200), default="")
    sample: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    content_type: Mapped[str] = mapped_column(String(40), default="")
    __table_args__ = (
        UniqueConstraint("oem_key", "reason", "field", "shape", name="uq_dlq_group"),
        Index("ix_dlq_last_seen", "last_seen"),
    )


class DlqSample(Base):
    """A bounded number of example payloads per group, for the agent and the UI."""
    __tablename__ = "dlq_sample"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("dlq_group.id", ondelete="CASCADE"), index=True)
    payload: Mapped[bytes] = mapped_column(LargeBinary)
    device: Mapped[str] = mapped_column(String(64), default="")
    ts: Mapped[int] = mapped_column(BigInteger)
    tracked: Mapped[bool] = mapped_column(Boolean, default=False)


class ReplayJob(Base):
    __tablename__ = "replay_job"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    oem_key: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True)
    scanned: Mapped[int] = mapped_column(BigInteger, default=0)
    republished: Mapped[int] = mapped_column(BigInteger, default=0)
    skipped: Mapped[int] = mapped_column(BigInteger, default=0)
    trigger: Mapped[str] = mapped_column(String(24), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --------------------------------------------------------- compliance and audit
class AuditLog(Base):
    """Append-only and hash-chained: each row stores the hash of the row before it,
    so removing or editing a row breaks the chain and is detectable."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor_kind: Mapped[str] = mapped_column(String(12))   # user | agent | service
    actor: Mapped[str] = mapped_column(String(200))
    tenant_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    resource: Mapped[str] = mapped_column(String(200))
    outcome: Mapped[str] = mapped_column(String(16), default="ok")
    ip: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        Index("ix_audit_ts_id", "ts", "id"),
        Index("ix_audit_actor", "actor", "id"),
        Index("ix_audit_tenant_id", "tenant_id", "id"),
    )


class ErasureRequest(Base):
    __tablename__ = "erasure_request"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"), index=True)
    subject_kind: Mapped[str] = mapped_column(String(16))  # driver
    subject_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    legal_basis: Mapped[str] = mapped_column(String(32), default="DPDP s.12 / GDPR art.17")
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


# -------------------------------------------------------------------- rollups
class MetricMinute(Base):
    __tablename__ = "metric_minute"
    oem_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    minute: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # epoch minute
    received: Mapped[int] = mapped_column(BigInteger, default=0)
    ok: Mapped[int] = mapped_column(BigInteger, default=0)
    failed: Mapped[int] = mapped_column(BigInteger, default=0)
    duplicates: Mapped[int] = mapped_column(BigInteger, default=0)
    p50_ms: Mapped[float] = mapped_column(Float, default=0.0)
    p95_ms: Mapped[float] = mapped_column(Float, default=0.0)
    p99_ms: Mapped[float] = mapped_column(Float, default=0.0)
    __table_args__ = (Index("ix_metric_minute_minute", "minute"),)
