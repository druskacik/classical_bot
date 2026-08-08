"""require GeoNames city identities and merge duplicate registry rows

Revision ID: 20260808000100
Revises: 20260807000100
Create Date: 2026-08-08 00:01:00
"""

import re
import unicodedata
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260808000100"
down_revision: Union[str, None] = "20260807000100"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


USTI_IDENTITIES = {
    ("geonames", "11711652"),  # airfield, incorrectly stored as the city
    ("geonames", "3063547"),  # district, incorrectly stored as the city
    ("geonames", "3063548"),  # populated place
    ("ruian", "554804"),  # municipality identity being replaced
}
USTI_GEONAMES_ID = "3063548"
USTI_SOURCE_URL = "https://www.geonames.org/3063548/usti-nad-labem.html"


def _normalize_city_key(value: str) -> str:
    return unicodedata.normalize("NFKC", re.sub(r"\s+", " ", value).strip()).casefold()


def _ensure_alias(
    connection,
    city_id: int,
    alias: str,
    *,
    source_url: str,
    language_code: str | None = None,
) -> None:
    normalized_alias = _normalize_city_key(alias)
    if not normalized_alias:
        return
    exists = connection.execute(
        sa.text(
            """
            SELECT 1 FROM city_alias
            WHERE city_id = :city_id AND normalized_alias = :normalized_alias
              AND source_scope IS NULL
            """
        ),
        {"city_id": city_id, "normalized_alias": normalized_alias},
    ).first()
    if exists:
        return
    connection.execute(
        sa.text(
            """
            INSERT INTO city_alias
                (city_id, alias, normalized_alias, language_code, alias_kind,
                 source_scope, source_url, created_by)
            VALUES
                (:city_id, :alias, :normalized_alias, :language_code,
                 'legitimate_name', NULL, :source_url, 'migration')
            """
        ),
        {
            "city_id": city_id,
            "alias": alias,
            "normalized_alias": normalized_alias,
            "language_code": language_code,
            "source_url": source_url,
        },
    )


def _copy_aliases(connection, source_id: int, target_id: int) -> None:
    aliases = connection.execute(
        sa.text(
            """
            SELECT alias, normalized_alias, language_code, alias_kind, source_scope,
                   source_url, created_by
            FROM city_alias WHERE city_id = :source_id ORDER BY id
            """
        ),
        {"source_id": source_id},
    ).mappings()
    for alias in aliases:
        exists = connection.execute(
            sa.text(
                """
                SELECT 1 FROM city_alias
                WHERE city_id = :target_id
                  AND normalized_alias = :normalized_alias
                  AND source_scope IS NOT DISTINCT FROM :source_scope
                """
            ),
            {
                "target_id": target_id,
                "normalized_alias": alias["normalized_alias"],
                "source_scope": alias["source_scope"],
            },
        ).first()
        if exists:
            continue
        connection.execute(
            sa.text(
                """
                INSERT INTO city_alias
                    (city_id, alias, normalized_alias, language_code, alias_kind,
                     source_scope, source_url, created_by)
                VALUES
                    (:target_id, :alias, :normalized_alias, :language_code,
                     :alias_kind, :source_scope, :source_url, :created_by)
                """
            ),
            {"target_id": target_id, **dict(alias)},
        )


def _merge_city(connection, source_id: int, target_id: int) -> None:
    if source_id == target_id:
        return
    rows = connection.execute(
        sa.text(
            """
            SELECT id, english_name, local_name, country_code, source_url
            FROM city WHERE id IN (:source_id, :target_id) ORDER BY id
            """
        ),
        {"source_id": source_id, "target_id": target_id},
    ).mappings().all()
    if len(rows) != 2:
        raise RuntimeError(
            f"Cannot merge city {source_id} into {target_id}: expected both rows"
        )
    by_id = {row["id"]: row for row in rows}
    source = by_id[source_id]
    target = by_id[target_id]
    if source["country_code"] != target["country_code"]:
        raise RuntimeError(
            f"Refusing cross-country city merge {source_id} -> {target_id}"
        )

    _copy_aliases(connection, source_id, target_id)
    _ensure_alias(
        connection,
        target_id,
        source["english_name"],
        source_url=source["source_url"],
    )
    _ensure_alias(
        connection,
        target_id,
        source["local_name"],
        source_url=source["source_url"],
    )
    for table in ("classical_concert", "potential_event"):
        connection.execute(
            sa.text(f"UPDATE {table} SET city_id = :target_id WHERE city_id = :source_id"),
            {"source_id": source_id, "target_id": target_id},
        )
    connection.execute(
        sa.text("DELETE FROM city_alias WHERE city_id = :source_id"),
        {"source_id": source_id},
    )
    connection.execute(
        sa.text("DELETE FROM city WHERE id = :source_id"),
        {"source_id": source_id},
    )


def _merge_case_duplicates(connection) -> None:
    duplicate_groups = connection.execute(
        sa.text(
            """
            SELECT lower(trim(external_source)) AS source_key, external_id
            FROM city
            GROUP BY lower(trim(external_source)), external_id
            HAVING COUNT(*) > 1
            ORDER BY source_key, external_id
            """
        )
    ).all()
    for source_key, external_id in duplicate_groups:
        if source_key != "geonames":
            raise RuntimeError(
                f"Unreviewed duplicate city provider {source_key!r} / {external_id}"
            )
        rows = connection.execute(
            sa.text(
                """
                SELECT id, country_code, english_name FROM city
                WHERE lower(trim(external_source)) = :source_key
                  AND external_id = :external_id
                ORDER BY id
                """
            ),
            {"source_key": source_key, "external_id": external_id},
        ).all()
        if len({row[1] for row in rows}) != 1:
            raise RuntimeError(
                f"Conflicting countries for GeoNames city {external_id}"
            )
        if len({_normalize_city_key(row[2]) for row in rows}) != 1:
            raise RuntimeError(
                f"Conflicting names for GeoNames city {external_id}"
            )
        target_id = rows[0][0]
        for source_id, _country, _english_name in rows[1:]:
            _merge_city(connection, source_id, target_id)


def _correct_usti_nad_labem(connection) -> None:
    rows = connection.execute(
        sa.text(
            """
            SELECT id, english_name, country_code, lower(trim(external_source)), external_id
            FROM city
            WHERE (lower(trim(external_source)), external_id) IN (
                ('geonames', '11711652'), ('geonames', '3063547'),
                ('geonames', '3063548'), ('ruian', '554804')
            )
            ORDER BY id
            """
        )
    ).all()
    if not rows:
        return
    for row in rows:
        if row[2] != "CZ" or _normalize_city_key(row[1]) != "ústí nad labem":
            raise RuntimeError(f"Unexpected Ústí nad Labem candidate: {row!r}")
        if (row[3], row[4]) not in USTI_IDENTITIES:
            raise RuntimeError(f"Unreviewed Ústí nad Labem identity: {row!r}")

    target_id = rows[0][0]
    for row in rows[1:]:
        _merge_city(connection, row[0], target_id)
    connection.execute(
        sa.text(
            """
            UPDATE city
            SET english_name = 'Ústí nad Labem', local_name = 'Ústí nad Labem',
                external_source = 'geonames', external_id = :external_id,
                source_url = :source_url
            WHERE id = :target_id
            """
        ),
        {
            "target_id": target_id,
            "external_id": USTI_GEONAMES_ID,
            "source_url": USTI_SOURCE_URL,
        },
    )
    _ensure_alias(
        connection,
        target_id,
        "Ústí nad Labem",
        source_url=USTI_SOURCE_URL,
        language_code="cs",
    )


def _normalize_and_assert_sources(connection) -> None:
    connection.execute(
        sa.text(
            "UPDATE city SET external_source = 'geonames' "
            "WHERE lower(trim(external_source)) = 'geonames'"
        )
    )
    rows = connection.execute(
        sa.text("SELECT id, external_source, external_id FROM city ORDER BY id")
    ).all()
    invalid = [
        row
        for row in rows
        if row[1] != "geonames" or re.fullmatch(r"[1-9][0-9]*", row[2]) is None
    ]
    if invalid:
        raise RuntimeError(f"Unreviewed non-GeoNames city identities: {invalid!r}")


def upgrade() -> None:
    connection = op.get_bind()
    _merge_case_duplicates(connection)
    _correct_usti_nad_labem(connection)
    _normalize_and_assert_sources(connection)

    op.drop_constraint("uq_city_external_identity", "city", type_="unique")
    op.create_unique_constraint("uq_city_geonames_id", "city", ["external_id"])
    op.create_check_constraint(
        "ck_city_external_source_geonames", "city", "external_source = 'geonames'"
    )
    op.create_check_constraint(
        "ck_city_external_id_numeric", "city", "external_id ~ '^[1-9][0-9]*$'"
    )


def downgrade() -> None:
    op.drop_constraint("ck_city_external_id_numeric", "city", type_="check")
    op.drop_constraint("ck_city_external_source_geonames", "city", type_="check")
    op.drop_constraint("uq_city_geonames_id", "city", type_="unique")
    op.create_unique_constraint(
        "uq_city_external_identity", "city", ["external_source", "external_id"]
    )
    # Reviewed identity corrections and merges intentionally remain in place.
