import importlib
import unittest

import sqlalchemy as sa


migration = importlib.import_module(
    "db.migrations.versions.20260808000100_require_geonames_city_identities"
)


class CityIdentityMigrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = sa.create_engine("sqlite://")
        self.connection = self.engine.connect()
        for statement in (
            "CREATE TABLE city (id INTEGER PRIMARY KEY, english_name TEXT NOT NULL, local_name TEXT NOT NULL, country_code TEXT NOT NULL, external_source TEXT NOT NULL, external_id TEXT NOT NULL, source_url TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT 'seed')",
            "CREATE TABLE city_alias (id INTEGER PRIMARY KEY, city_id INTEGER NOT NULL REFERENCES city(id), alias TEXT NOT NULL, normalized_alias TEXT NOT NULL, language_code TEXT, alias_kind TEXT NOT NULL, source_scope TEXT, source_url TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT 'seed')",
            "CREATE TABLE classical_concert (id INTEGER PRIMARY KEY, city_id INTEGER)",
            "CREATE TABLE potential_event (id INTEGER PRIMARY KEY, city_id INTEGER)",
        ):
            self.connection.exec_driver_sql(statement)

    def tearDown(self):
        self.connection.close()
        self.engine.dispose()

    def _city(self, city_id, english, local, country, source, external_id):
        self.connection.exec_driver_sql(
            "INSERT INTO city VALUES (?, ?, ?, ?, ?, ?, ?, 'test')",
            (
                city_id, english, local, country, source, external_id,
                f"https://example.test/{external_id}",
            ),
        )

    def _alias(self, alias_id, city_id, alias, normalized, scope=None):
        self.connection.exec_driver_sql(
            "INSERT INTO city_alias VALUES (?, ?, ?, ?, NULL, 'legitimate_name', ?, 'https://example.test', 'test')",
            (alias_id, city_id, alias, normalized, scope),
        )

    def test_merges_case_duplicates_and_corrects_usti(self):
        self._city(1, "Prague", "Praha", "CZ", "geonames", "3067696")
        self._city(60, "Prague", "Praha", "CZ", "GeoNames", "3067696")
        self._city(79, "Taipei", "臺北市", "TW", "GeoNames", "1668341")
        self._city(85, "Taipei", "臺北", "TW", "geonames", "1668341")
        self._city(61, "Ústí nad Labem", "Ústí nad Labem", "CZ", "GeoNames", "11711652")
        self._city(66, "Ústí nad Labem", "Ústí nad Labem", "CZ", "GeoNames", "3063547")
        self._city(72, "Ústí nad Labem", "Ústí nad Labem", "CZ", "RUIAN", "554804")
        self._alias(1, 1, "Praha", "praha")
        self._alias(2, 60, "Praha", "praha")
        self._alias(3, 85, "Taipei", "taipei")
        self._alias(4, 72, "Ústí nad Labem", "ústí nad labem")
        self.connection.exec_driver_sql(
            "INSERT INTO classical_concert VALUES (1, 60), (2, 66), (3, 72)"
        )
        self.connection.exec_driver_sql("INSERT INTO potential_event VALUES (1, 85)")

        migration._merge_case_duplicates(self.connection)
        migration._correct_usti_nad_labem(self.connection)
        migration._normalize_and_assert_sources(self.connection)

        self.assertEqual(
            self.connection.exec_driver_sql(
                "SELECT id FROM city ORDER BY id"
            ).fetchall(),
            [(1,), (61,), (79,)],
        )
        self.assertEqual(
            self.connection.exec_driver_sql(
                "SELECT external_source, external_id, source_url FROM city WHERE id = 61"
            ).one(),
            ("geonames", "3063548", migration.USTI_SOURCE_URL),
        )
        self.assertEqual(
            self.connection.exec_driver_sql(
                "SELECT city_id FROM classical_concert ORDER BY id"
            ).fetchall(),
            [(1,), (61,), (61,)],
        )
        self.assertEqual(
            self.connection.exec_driver_sql(
                "SELECT city_id FROM potential_event"
            ).scalar(),
            79,
        )
        self.assertEqual(
            self.connection.exec_driver_sql(
                "SELECT COUNT(*) FROM city_alias WHERE city_id = 1 AND normalized_alias = 'praha'"
            ).scalar(),
            1,
        )
        self.assertEqual(
            self.connection.exec_driver_sql(
                "SELECT COUNT(*) FROM city_alias WHERE city_id = 79 AND normalized_alias = '臺北'"
            ).scalar(),
            1,
        )

    def test_refuses_cross_country_duplicate_identity(self):
        self._city(1, "Example", "Example", "CZ", "geonames", "123")
        self._city(2, "Example", "Example", "SK", "GeoNames", "123")

        with self.assertRaisesRegex(RuntimeError, "Conflicting countries"):
            migration._merge_case_duplicates(self.connection)

    def test_refuses_unreviewed_non_geonames_identity(self):
        self._city(1, "Example", "Example", "CZ", "wikidata", "123")

        with self.assertRaisesRegex(RuntimeError, "non-GeoNames"):
            migration._normalize_and_assert_sources(self.connection)


if __name__ == "__main__":
    unittest.main()
