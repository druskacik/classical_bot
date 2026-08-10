import unittest
from unittest.mock import MagicMock, patch

from agent_utils import potential_event_catalog as catalog


class PotentialEventCatalogTests(unittest.TestCase):
    def database(self, *, rows, description):
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchall.return_value = rows
        cursor.description = [(name,) for name in description]
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value = cursor
        return connection, cursor

    def test_list_events_scopes_results_to_source_and_source_url(self):
        connection, cursor = self.database(
            rows=[(7, "Concert")],
            description=["id", "title"],
        )
        with patch.object(catalog, "get_connection", return_value=connection):
            rows = catalog.list_events(
                "Example Hall",
                "https://example.test",
                limit=25,
                after_id=4,
                unclassified_only=True,
            )

        query, params = cursor.execute.call_args.args
        self.assertIn("source IS NOT DISTINCT FROM %s", query)
        self.assertIn("source_url IS NOT DISTINCT FROM %s", query)
        self.assertIn("analyzed = false", query)
        self.assertEqual(params, ["Example Hall", "https://example.test", 4, 25])
        self.assertEqual(rows, [{"id": 7, "title": "Concert"}])

    def test_event_lookup_uses_parameterized_id_array(self):
        connection, cursor = self.database(
            rows=[],
            description=["id"],
        )
        with patch.object(catalog, "get_connection", return_value=connection):
            catalog.get_events([2, 3])

        query, params = cursor.execute.call_args.args
        self.assertIn("id = ANY(%s)", query)
        self.assertEqual(params, ([2, 3],))


if __name__ == "__main__":
    unittest.main()
