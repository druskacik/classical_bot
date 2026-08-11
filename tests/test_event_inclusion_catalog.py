import unittest
from unittest.mock import MagicMock, patch

from agent_utils import event_inclusion_catalog as catalog


class EventInclusionCatalogTests(unittest.TestCase):
    @patch.object(catalog, "get_connection")
    def test_summary_reports_statuses_and_assessments(self, get_connection):
        connection = MagicMock()
        cursor = MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value.__enter__.return_value = cursor
        get_connection.return_value = connection
        cursor.description = [("origin",), ("decision",), ("assessments",)]
        cursor.fetchall.side_effect = [
            [("included", 10), ("quarantined", 2)],
            [("programme_analyzer", "not_event", 2)],
        ]

        def execute(query, _params=None):
            if "FROM classical_concert" in query:
                cursor.description = [("inclusion_status",), ("concerts",)]
            else:
                cursor.description = [("origin",), ("decision",), ("assessments",)]

        cursor.execute.side_effect = execute
        result = catalog.summary(prod=True)

        get_connection.assert_called_once_with(prod=True)
        self.assertEqual(result["concerts"][1]["concerts"], 2)
        self.assertEqual(result["assessments"][0]["decision"], "not_event")

    @patch.object(catalog, "get_connection")
    def test_conflicts_require_opposing_decisions(self, get_connection):
        connection = MagicMock()
        cursor = MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value.__enter__.return_value = cursor
        get_connection.return_value = connection
        cursor.description = [("id",)]
        cursor.fetchall.return_value = []

        catalog.list_conflicts(limit=25)

        query, params = cursor.execute.call_args.args
        self.assertIn("BOOL_OR(e.decision = 'classical')", query)
        self.assertIn("nonclassical", query)
        self.assertEqual(params, (25,))


if __name__ == "__main__":
    unittest.main()
