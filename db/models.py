from sqlalchemy import (
    ARRAY,
    Boolean,
    BigInteger,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Text,
    Time,
    UniqueConstraint,
    CheckConstraint,
    Index,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base


metadata = MetaData()
Base = declarative_base(metadata=metadata)


class ClassicalConcert(Base):
    __tablename__ = "classical_concert"
    __table_args__ = (
        CheckConstraint(
            "event_status IN ('scheduled', 'cancelled', 'postponed', 'rescheduled')",
            name="ck_classical_concert_event_status",
        ),
        CheckConstraint(
            "country_code_resolved IS NULL OR country_code_resolved ~ '^[A-Z]{2}$'",
            name="ck_classical_concert_country_code_resolved",
        ),
        Index("ix_classical_concert_date_time_id", "date", "time_from", "id"),
        Index(
            "ix_classical_concert_country_date_time_id",
            "country_code_resolved",
            "date",
            "time_from",
            "id",
        ),
        Index(
            "ix_classical_concert_location_date",
            "country_code_resolved",
            "city_id",
            "date",
        ),
    )

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    date = Column(Date, nullable=False)
    url = Column(String, nullable=False)
    source = Column(String)
    source_url = Column(String)
    time_from = Column(Time)
    time_to = Column(Time)
    city_raw = Column(String)
    country_code_raw = Column(String(2))
    city_id = Column(BigInteger, ForeignKey("city.id", ondelete="SET NULL"))
    country_code_resolved = Column(String(2))
    venue = Column(String)
    type = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())
    description = Column(Text)
    is_concert_details_filled = Column(Boolean, server_default="false")
    composers = Column(ARRAY(Text))
    program_analysis_eligible = Column(Boolean, nullable=False, server_default="true")
    event_status = Column(String, nullable=False, server_default="scheduled")
    event_status_updated_at = Column(DateTime(timezone=True))
    last_verified_at = Column(DateTime(timezone=True))


class PotentialEvent(Base):
    __tablename__ = "potential_event"
    __table_args__ = (
        CheckConstraint(
            "country_code_resolved IS NULL OR country_code_resolved ~ '^[A-Z]{2}$'",
            name="ck_potential_event_country_code_resolved",
        ),
        Index("ix_potential_event_city_id", "city_id"),
    )

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    date = Column(Date, nullable=False)
    url = Column(String, nullable=False)
    source = Column(String)
    source_url = Column(String)
    time_from = Column(Time)
    time_to = Column(Time)
    city_raw = Column(String)
    country_code_raw = Column(String(2))
    city_id = Column(BigInteger, ForeignKey("city.id", ondelete="SET NULL"))
    country_code_resolved = Column(String(2))
    venue = Column(String)
    type = Column(String)
    analyzed = Column(Boolean, server_default="false")
    is_classical_concert = Column(Boolean, server_default="false")
    added = Column(Boolean, server_default="false")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())
    description = Column(Text)
    is_concert_details_filled = Column(Boolean, server_default="false")
    composers = Column(ARRAY(Text))


class City(Base):
    __tablename__ = "city"
    __table_args__ = (
        UniqueConstraint("external_source", "external_id", name="uq_city_external_identity"),
        UniqueConstraint("external_id", name="uq_city_geonames_id"),
        CheckConstraint("country_code ~ '^[A-Z]{2}$'", name="ck_city_country_code"),
        CheckConstraint("external_source = 'geonames'", name="ck_city_external_source_geonames"),
        CheckConstraint("external_id ~ '^[1-9][0-9]*$'", name="ck_city_external_id_numeric"),
        Index("ix_city_country_english_name", "country_code", "english_name"),
    )

    id = Column(BigInteger, primary_key=True)
    english_name = Column(Text, nullable=False)
    local_name = Column(Text, nullable=False)
    country_code = Column(String(2), nullable=False)
    external_source = Column(String, nullable=False)
    external_id = Column(String, nullable=False)
    source_url = Column(Text, nullable=False)
    created_by = Column(String, nullable=False, server_default="seed")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CityAlias(Base):
    __tablename__ = "city_alias"
    __table_args__ = (
        CheckConstraint(
            "alias_kind IN ('legitimate_name', 'extraction_artifact')",
            name="ck_city_alias_kind",
        ),
        Index("ix_city_alias_lookup", "normalized_alias"),
        Index(
            "uq_city_alias_global",
            "city_id",
            "normalized_alias",
            unique=True,
            postgresql_where=text("source_scope IS NULL"),
        ),
        Index(
            "uq_city_alias_scoped",
            "city_id",
            "normalized_alias",
            "source_scope",
            unique=True,
            postgresql_where=text("source_scope IS NOT NULL"),
        ),
    )

    id = Column(BigInteger, primary_key=True)
    city_id = Column(BigInteger, ForeignKey("city.id", ondelete="CASCADE"), nullable=False)
    alias = Column(Text, nullable=False)
    normalized_alias = Column(Text, nullable=False)
    language_code = Column(String)
    alias_kind = Column(String, nullable=False)
    source_scope = Column(String)
    source_url = Column(Text, nullable=False)
    created_by = Column(String, nullable=False, server_default="seed")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Composer(Base):
    __tablename__ = "composer"
    __table_args__ = (UniqueConstraint("normalized_name", name="uq_composer_normalized_name"),)

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    normalized_name = Column(String, nullable=False)


class ComposerAlias(Base):
    __tablename__ = "composer_alias"
    __table_args__ = (
        UniqueConstraint(
            "composer_id",
            "normalized_alias",
            name="uq_composer_alias_composer_normalized",
        ),
        Index("ix_composer_alias_normalized", "normalized_alias"),
    )

    id = Column(Integer, primary_key=True)
    composer_id = Column(Integer, ForeignKey("composer.id", ondelete="CASCADE"), nullable=False)
    alias = Column(Text, nullable=False)
    normalized_alias = Column(Text, nullable=False)
    language_code = Column(String(2))


class ClassicalConcertComposer(Base):
    __tablename__ = "classical_concert_composer"
    __table_args__ = (
        UniqueConstraint(
            "classical_concert_id",
            "composer_id",
            name="uq_classical_concert_composer_link",
        ),
        Index(
            "ix_classical_concert_composer_composer_concert",
            "composer_id",
            "classical_concert_id",
        ),
    )

    id = Column(Integer, primary_key=True)
    classical_concert_id = Column(Integer, ForeignKey("classical_concert.id"))
    composer_id = Column(Integer, ForeignKey("composer.id"))


class Work(Base):
    __tablename__ = "work"
    __table_args__ = (
        UniqueConstraint("composer_id", "normalized_title", name="uq_work_composer_title"),
    )

    id = Column(Integer, primary_key=True)
    composer_id = Column(Integer, ForeignKey("composer.id"), nullable=False)
    title = Column(String, nullable=False)
    normalized_title = Column(String, nullable=False)
    catalogue_number = Column(String)


class ClassicalConcertWork(Base):
    __tablename__ = "classical_concert_work"
    __table_args__ = (
        UniqueConstraint(
            "classical_concert_id",
            "work_id",
            name="uq_classical_concert_work_link",
        ),
        Index(
            "ix_classical_concert_work_work_concert",
            "work_id",
            "classical_concert_id",
        ),
    )

    id = Column(Integer, primary_key=True)
    classical_concert_id = Column(Integer, ForeignKey("classical_concert.id"), nullable=False)
    work_id = Column(Integer, ForeignKey("work.id"), nullable=False)
    programme_label = Column(Text, nullable=False)
    source_url = Column(Text, nullable=False)
    evidence = Column(Text)


class ConcertProgramAnalysis(Base):
    __tablename__ = "concert_program_analysis"
    __table_args__ = (
        UniqueConstraint(
            "classical_concert_id",
            name="uq_concert_program_analysis_concert",
        ),
    )

    id = Column(Integer, primary_key=True)
    classical_concert_id = Column(Integer, ForeignKey("classical_concert.id"), nullable=False)
    status = Column(String, nullable=False)
    attempts = Column(Integer, nullable=False, server_default="0")
    model = Column(String)
    raw_result = Column(JSONB)
    last_error = Column(Text)
    last_attempted_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))


class ClassicalConcertChange(Base):
    __tablename__ = "classical_concert_change"
    __table_args__ = (
        CheckConstraint(
            "field_name IN ('event_status', 'date', 'time_from', 'time_to', "
            "'city', 'country_code', 'city_id', 'country_code_resolved', 'venue')",
            name="ck_classical_concert_change_field",
        ),
        Index(
            "ix_classical_concert_change_concert_created",
            "classical_concert_id",
            "created_at",
        ),
    )

    id = Column(BigInteger, primary_key=True)
    classical_concert_id = Column(
        Integer,
        ForeignKey("classical_concert.id", ondelete="CASCADE"),
        nullable=False,
    )
    field_name = Column(String, nullable=False)
    old_value = Column(JSONB)
    new_value = Column(JSONB, nullable=False)
    source_url = Column(Text, nullable=False)
    evidence = Column(Text, nullable=False)
    model = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


CRAWLER_SOURCE_STATUSES = (
    "pending",
    "processing",
    "pr_open",
    "active",
    "blocked",
    "retry_wait",
    "duplicate",
    "needs_attention",
    "disabled",
)


class CrawlerSource(Base):
    __tablename__ = "crawler_source"
    __table_args__ = (
        CheckConstraint(
            f"status IN {CRAWLER_SOURCE_STATUSES!r}",
            name="ck_crawler_source_status",
        ),
        CheckConstraint("priority >= 0", name="ck_crawler_source_priority"),
        CheckConstraint(
            "geographic_scope IN ('unknown', 'country', 'multi_country')",
            name="ck_crawler_source_geographic_scope",
        ),
        CheckConstraint(
            "geographic_scope = 'unknown' OR "
            "(geographic_scope = 'country' AND country_code IS NOT NULL AND "
            "crawler_path IS NOT NULL AND country_code ~ '^[A-Z]{2}$' AND "
            "crawler_path ~ ('^crawlers/' || lower(country_code) || '/[^/]+$')) OR "
            "(geographic_scope = 'multi_country' AND country_code IS NULL AND "
            "crawler_path IS NOT NULL AND "
            "crawler_path ~ '^crawlers/common/[^/]+$')",
            name="ck_crawler_source_geographic_identity",
        ),
        UniqueConstraint("crawler_path", name="uq_crawler_source_crawler_path"),
        Index("ix_crawler_source_due", "status", "next_attempt_at", "priority"),
        Index("ix_crawler_source_lease", "lease_expires_at"),
    )

    id = Column(BigInteger, primary_key=True)
    country_code = Column(String(2))
    geographic_scope = Column(String, nullable=False, server_default="unknown")
    canonical_url = Column(Text, nullable=False)
    crawler_path = Column(Text)
    status = Column(String, nullable=False, server_default="pending")
    priority = Column(Integer, nullable=False, server_default="0")
    next_attempt_at = Column(DateTime(timezone=True))
    lease_owner = Column(String)
    lease_expires_at = Column(DateTime(timezone=True))
    duplicate_of_id = Column(BigInteger, ForeignKey("crawler_source.id"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CrawlerSourceUrl(Base):
    __tablename__ = "crawler_source_url"
    __table_args__ = (
        CheckConstraint(
            "role IN ('submitted', 'canonical', 'redirect')",
            name="ck_crawler_source_url_role",
        ),
        UniqueConstraint("normalized_url", name="uq_crawler_source_url_normalized"),
        Index("ix_crawler_source_url_source", "crawler_source_id"),
    )

    id = Column(BigInteger, primary_key=True)
    crawler_source_id = Column(BigInteger, ForeignKey("crawler_source.id", ondelete="CASCADE"), nullable=False)
    url = Column(Text, nullable=False)
    normalized_url = Column(Text, nullable=False)
    role = Column(String, nullable=False)
    discovered_by = Column(String, nullable=False)
    metadata_json = Column(JSONB)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CrawlerFactoryRun(Base):
    __tablename__ = "crawler_factory_run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'no_changes', 'pr_open', 'completed', 'failed')",
            name="ck_crawler_factory_run_status",
        ),
    )

    id = Column(String, primary_key=True)
    worker_id = Column(String, nullable=False)
    branch = Column(Text)
    pull_request_url = Column(Text)
    model = Column(String)
    status = Column(String, nullable=False, server_default="running")
    started_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at = Column(DateTime(timezone=True))


class CrawlerSourceAttempt(Base):
    __tablename__ = "crawler_source_attempt"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('running', 'generated', 'blocked', 'generation_failed', "
            "'duplicate', 'skipped_existing', 'abandoned')",
            name="ck_crawler_source_attempt_outcome",
        ),
        Index("ix_crawler_source_attempt_source_started", "crawler_source_id", "started_at"),
    )

    id = Column(BigInteger, primary_key=True)
    crawler_source_id = Column(BigInteger, ForeignKey("crawler_source.id", ondelete="CASCADE"), nullable=False)
    run_id = Column(String, ForeignKey("crawler_factory_run.id", ondelete="CASCADE"), nullable=False)
    attempted_url = Column(Text, nullable=False)
    resolved_url = Column(Text)
    crawler_path = Column(Text)
    outcome = Column(String, nullable=False, server_default="running")
    commit_sha = Column(String)
    pull_request_url = Column(Text)
    generation_warning = Column(Text)
    error = Column(Text)
    retry_after = Column(DateTime(timezone=True))
    started_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at = Column(DateTime(timezone=True))


class CrawlerSourceSeed(Base):
    __tablename__ = "crawler_source_seed"

    filename = Column(Text, primary_key=True)
    sha256 = Column(String(64), nullable=False)
    row_count = Column(Integer, nullable=False)
    applied_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
