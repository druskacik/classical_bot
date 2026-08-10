import unittest
from unittest.mock import MagicMock, patch

from crawlers import classical


class ClassicalUploadLockTests(unittest.TestCase):
    def connection(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (1,)
        connection = MagicMock()
        connection.cursor.return_value = cursor
        return connection, cursor

    def test_direct_classical_upload_uses_shared_transaction_lock(self):
        connection, cursor = self.connection()
        with patch.object(classical.psycopg2, "connect", return_value=connection):
            classical.upload_concerts([])

        query, params = cursor.execute.call_args_list[0].args
        self.assertIn("pg_advisory_xact_lock", query)
        self.assertEqual(params, (classical.CONCERT_INSERT_ADVISORY_LOCK,))

    def test_potential_upload_does_not_take_classical_insert_lock(self):
        connection, cursor = self.connection()
        with patch.object(classical.psycopg2, "connect", return_value=connection):
            classical.upload_potential_concerts([])

        self.assertFalse(
            any("pg_advisory_xact_lock" in call.args[0] for call in cursor.execute.call_args_list)
        )


if __name__ == "__main__":
    unittest.main()
