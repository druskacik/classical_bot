import unittest
from unittest.mock import Mock, patch

import psycopg2

from automation.crawler_registry import CrawlerRegistry, get_connection


class FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None


class FakeConnection:
    def __init__(self, *, closed=0, cursor_error=None):
        self.closed = closed
        self.cursor_error = cursor_error
        self.close_calls = 0
        self.cursor_calls = 0

    def close(self):
        self.close_calls += 1
        self.closed = 1

    def cursor(self, *, cursor_factory):
        self.cursor_calls += 1
        if self.cursor_error is not None:
            raise self.cursor_error
        return FakeCursor()


class ConnectionConfigurationTests(unittest.TestCase):
    @patch("automation.crawler_registry.psycopg2.connect")
    @patch.dict(
        "os.environ",
        {
            "DB_NAME": "classical",
            "DB_USER": "factory",
            "DB_PASS": "secret",
            "DB_HOST": "database",
            "DB_PORT": "5432",
        },
        clear=True,
    )
    def test_connection_enables_short_tcp_keepalives(self, connect):
        get_connection()

        connect.assert_called_once_with(
            dbname="classical",
            user="factory",
            password="secret",
            host="database",
            port="5432",
            keepalives=1,
            keepalives_idle=60,
            keepalives_interval=20,
            keepalives_count=3,
        )


class RegistryReconnectTests(unittest.TestCase):
    def test_owned_closed_connection_is_replaced_before_cursor_use(self):
        closed = FakeConnection(closed=1)
        replacement = FakeConnection()
        factory = Mock(side_effect=[closed, replacement])
        registry = CrawlerRegistry(connection_factory=factory)

        with registry.cursor():
            pass

        self.assertIs(registry.connection, replacement)
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(replacement.cursor_calls, 1)

    def test_cursor_interface_error_reconnects_once(self):
        failed = FakeConnection(cursor_error=psycopg2.InterfaceError("closed"))
        replacement = FakeConnection()
        factory = Mock(side_effect=[failed, replacement])
        registry = CrawlerRegistry(connection_factory=factory)

        with registry.cursor():
            pass

        self.assertEqual(factory.call_count, 2)
        self.assertEqual(failed.close_calls, 1)
        self.assertEqual(replacement.cursor_calls, 1)

    def test_cursor_operational_error_reconnects_once(self):
        failed = FakeConnection(cursor_error=psycopg2.OperationalError("network"))
        replacement = FakeConnection()
        factory = Mock(side_effect=[failed, replacement])
        registry = CrawlerRegistry(connection_factory=factory)

        with registry.cursor():
            pass

        self.assertEqual(factory.call_count, 2)
        self.assertEqual(replacement.cursor_calls, 1)

    def test_replacement_cursor_failure_is_not_retried(self):
        failed = FakeConnection(cursor_error=psycopg2.InterfaceError("closed"))
        replacement = FakeConnection(
            cursor_error=psycopg2.OperationalError("still unavailable")
        )
        factory = Mock(side_effect=[failed, replacement])
        registry = CrawlerRegistry(connection_factory=factory)

        with self.assertRaisesRegex(psycopg2.OperationalError, "still unavailable"):
            with registry.cursor():
                pass

        self.assertEqual(factory.call_count, 2)
        self.assertEqual(replacement.cursor_calls, 1)

    def test_injected_connection_is_never_replaced_or_closed(self):
        injected = FakeConnection(cursor_error=psycopg2.InterfaceError("closed"))
        factory = Mock()
        registry = CrawlerRegistry(
            connection=injected,
            connection_factory=factory,
        )

        with self.assertRaisesRegex(psycopg2.InterfaceError, "closed"):
            with registry.cursor():
                pass
        registry.close()

        factory.assert_not_called()
        self.assertEqual(injected.close_calls, 0)


if __name__ == "__main__":
    unittest.main()
