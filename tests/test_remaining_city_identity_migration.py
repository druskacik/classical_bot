import importlib
import unittest
from unittest.mock import patch

import sqlalchemy as sa


migration = importlib.import_module(
    "db.migrations.versions.20260808000300_correct_remaining_city_identities"
)


class RemainingCityIdentityMigrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = sa.create_engine("sqlite://")
        self.connection = self.engine.connect()
        for statement in (
            "CREATE TABLE city (id INTEGER PRIMARY KEY, english_name TEXT NOT NULL, "
            "local_name TEXT NOT NULL, country_code TEXT NOT NULL, "
            "external_source TEXT NOT NULL, external_id TEXT NOT NULL, "
            "source_url TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT 'seed')",
            "CREATE TABLE city_alias (id INTEGER PRIMARY KEY, city_id INTEGER NOT NULL "
            "REFERENCES city(id), alias TEXT NOT NULL, normalized_alias TEXT NOT NULL, "
            "language_code TEXT, alias_kind TEXT NOT NULL, source_scope TEXT, "
            "source_url TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT 'seed')",
            "CREATE TABLE classical_concert "
            "(id INTEGER PRIMARY KEY, city_id INTEGER, city_raw TEXT)",
            "CREATE TABLE potential_event (id INTEGER PRIMARY KEY, city_id INTEGER, city_raw TEXT)",
        ):
            self.connection.exec_driver_sql(statement)
        for city_id, (name, country, external_id) in migration.EXPECTED_CITIES.items():
            self.connection.exec_driver_sql(
                "INSERT INTO city VALUES (?, ?, ?, ?, 'geonames', ?, ?, 'test')",
                (
                    city_id,
                    name,
                    name,
                    country,
                    external_id,
                    f"https://example.test/{external_id}",
                ),
            )
            self.connection.exec_driver_sql(
                "INSERT INTO city_alias VALUES "
                "(?, ?, ?, ?, NULL, 'legitimate_name', NULL, "
                "'https://example.test', 'test')",
                (city_id, city_id, name, name.casefold()),
            )

    def tearDown(self):
        self.connection.close()
        self.engine.dispose()

    def test_merges_reviewed_rows_and_splits_miami(self):
        concerts = (
            (1, 83, "Indianapolis"),
            (2, 84, "Indianapolis"),
            (3, 86, "Miami"),
            (4, 86, "Miami Beach"),
            (5, 89, "Miami Beach"),
            (6, 91, "Miami Beach"),
            (7, 92, "Miami Beach"),
            (8, 73, "Norrköping"),
            (9, 76, "Norrköping"),
            (10, 88, "Norrköping"),
            (11, 93, "Norrköping"),
            (12, 98, "Norrköping"),
        )
        self.connection.exec_driver_sql(
            "INSERT INTO classical_concert VALUES (?, ?, ?)", list(concerts)
        )
        self.connection.exec_driver_sql(
            "INSERT INTO potential_event VALUES (1, 86, 'Miami Beach'), (2, 84, 'Indianapolis')"
        )

        with patch.object(migration.op, "get_bind", return_value=self.connection):
            migration.upgrade()

        self.assertEqual(
            self.connection.exec_driver_sql(
                "SELECT id FROM city ORDER BY id"
            ).fetchall(),
            [(73,), (83,), (86,), (91,)],
        )
        self.assertEqual(
            self.connection.exec_driver_sql(
                "SELECT id, city_id FROM classical_concert ORDER BY id"
            ).fetchall(),
            [
                (1, 83), (2, 83), (3, 86), (4, 91), (5, 91), (6, 91),
                (7, 91), (8, 73), (9, 73), (10, 73), (11, 73), (12, 73),
            ],
        )
        self.assertEqual(
            self.connection.exec_driver_sql(
                "SELECT id, city_id FROM potential_event ORDER BY id"
            ).fetchall(),
            [(1, 91), (2, 83)],
        )
        self.assertEqual(
            self.connection.exec_driver_sql(
                "SELECT english_name, external_id, source_url FROM city WHERE id = 86"
            ).one(),
            ("Miami", "4164138", migration.CANONICAL[86][4]),
        )
        self.assertEqual(
            self.connection.exec_driver_sql(
                "SELECT normalized_alias FROM city_alias WHERE city_id = 86"
            ).fetchall(),
            [("miami",)],
        )

    def test_refuses_unexpected_registry_or_miami_raw_value(self):
        self.connection.exec_driver_sql(
            "UPDATE city SET external_id = '999' WHERE id = 84"
        )
        with self.assertRaisesRegex(RuntimeError, "Unexpected reviewed city rows"):
            migration._assert_expected_rows(self.connection)

        self.connection.exec_driver_sql(
            "UPDATE city SET external_id = '4926563' WHERE id = 84"
        )
        self.connection.exec_driver_sql(
            "INSERT INTO classical_concert VALUES (1, 86, 'Fort Lauderdale')"
        )
        with self.assertRaisesRegex(RuntimeError, "Miami raw values"):
            migration._split_miami(self.connection)


if __name__ == "__main__":
    unittest.main()
