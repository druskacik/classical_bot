"""correct remaining reviewed city identities

Revision ID: 20260808000300
Revises: 20260808000200
Create Date: 2026-08-08 00:03:00
"""

import re
import unicodedata
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260808000300"
down_revision: Union[str, None] = "20260808000200"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


EXPECTED_CITIES = {
    83: ("Indianapolis", "US", "4259418"),
    84: ("Indianapolis", "US", "4926563"),
    86: ("Miami Beach", "US", "4164138"),
    89: ("Miami Beach", "US", "4164167"),
    91: ("Miami Beach", "US", "4164143"),
    92: ("Miami Beach", "US", "8479092"),
    73: ("Norrköping", "SE", "2688368"),
    76: ("Norrköping", "SE", "3148675"),
    88: ("Norrköping", "SE", "2688367"),
    93: ("Norrköping", "SE", "3149100"),
    98: ("Norrköping", "SE", "2686657"),
}
CANONICAL = {
    83: (
        "Indianapolis",
        "Indianapolis",
        "US",
        "4259418",
        "https://www.geonames.org/4259418/indianapolis.html",
    ),
    86: ("Miami", "Miami", "US", "4164138", "https://www.geonames.org/4164138/miami.html"),
    91: (
        "Miami Beach",
        "Miami Beach",
        "US",
        "4164143",
        "https://www.geonames.org/4164143/miami-beach.html",
    ),
    73: (
        "Norrköping",
        "Norrköping",
        "SE",
        "2688368",
        "https://www.geonames.org/2688368/norrkoping.html",
    ),
}


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", re.sub(r"\s+", " ", value).strip()).casefold()


def _assert_expected_rows(connection) -> None:
    rows = connection.execute(
        sa.text(
            "SELECT id, english_name, country_code, external_id FROM city "
            "WHERE id IN (73, 76, 83, 84, 86, 88, 89, 91, 92, 93, 98)"
        )
    ).all()
    actual = {row[0]: (row[1], row[2], row[3]) for row in rows}
    if actual != EXPECTED_CITIES:
        raise RuntimeError(f"Unexpected reviewed city rows: {actual!r}")


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
        if not exists:
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


def _ensure_alias(connection, city_id: int, alias: str, source_url: str) -> None:
    normalized = _normalize(alias)
    exists = connection.execute(
        sa.text(
            "SELECT 1 FROM city_alias WHERE city_id = :city_id "
            "AND normalized_alias = :normalized AND source_scope IS NULL"
        ),
        {"city_id": city_id, "normalized": normalized},
    ).first()
    if not exists:
        connection.execute(
            sa.text(
                """
                INSERT INTO city_alias
                    (city_id, alias, normalized_alias, language_code, alias_kind,
                     source_scope, source_url, created_by)
                VALUES (:city_id, :alias, :normalized, NULL, 'legitimate_name',
                        NULL, :source_url, 'migration')
                """
            ),
            {
                "city_id": city_id,
                "alias": alias,
                "normalized": normalized,
                "source_url": source_url,
            },
        )


def _merge_city(connection, source_id: int, target_id: int) -> None:
    _copy_aliases(connection, source_id, target_id)
    for table in ("classical_concert", "potential_event"):
        connection.execute(
            sa.text(f"UPDATE {table} SET city_id = :target WHERE city_id = :source"),
            {"target": target_id, "source": source_id},
        )
    connection.execute(
        sa.text("DELETE FROM city_alias WHERE city_id = :source"),
        {"source": source_id},
    )
    connection.execute(
        sa.text("DELETE FROM city WHERE id = :source"), {"source": source_id}
    )


def _split_miami(connection) -> None:
    for table in ("classical_concert", "potential_event"):
        raw_values = connection.execute(
            sa.text(
                f"SELECT DISTINCT lower(trim(city_raw)) FROM {table} "
                "WHERE city_id = 86"
            )
        ).scalars()
        unexpected = sorted(
            (
                value
                for value in raw_values
                if value not in {"miami", "miami beach"}
            ),
            key=lambda value: "" if value is None else value,
        )
        if unexpected:
            raise RuntimeError(f"Unexpected {table} Miami raw values: {unexpected!r}")
        connection.execute(
            sa.text(
                f"UPDATE {table} SET city_id = 91 "
                "WHERE city_id = 86 AND lower(trim(city_raw)) = 'miami beach'"
            )
        )

    connection.execute(
        sa.text(
            "DELETE FROM city_alias WHERE city_id = 86 "
            "AND normalized_alias = 'miami beach'"
        )
    )
    _merge_city(connection, 89, 91)
    _merge_city(connection, 92, 91)


def _set_canonical_rows(connection) -> None:
    for city_id, (english, local, country, external_id, source_url) in CANONICAL.items():
        connection.execute(
            sa.text(
                """
                UPDATE city SET english_name = :english, local_name = :local,
                    country_code = :country, external_source = 'geonames',
                    external_id = :external_id, source_url = :source_url
                WHERE id = :city_id
                """
            ),
            {
                "city_id": city_id,
                "english": english,
                "local": local,
                "country": country,
                "external_id": external_id,
                "source_url": source_url,
            },
        )
        _ensure_alias(connection, city_id, local, source_url)


def upgrade() -> None:
    connection = op.get_bind()
    _assert_expected_rows(connection)
    _merge_city(connection, 84, 83)
    _merge_city(connection, 76, 73)
    _merge_city(connection, 88, 73)
    _merge_city(connection, 93, 73)
    _merge_city(connection, 98, 73)
    _split_miami(connection)
    _set_canonical_rows(connection)


def downgrade() -> None:
    # Reviewed identity corrections and merges intentionally remain in place.
    pass
