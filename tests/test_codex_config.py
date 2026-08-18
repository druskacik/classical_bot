import os
import unittest
from unittest.mock import patch

from automation.codex_config import ephemeral_from_environment


class CodexConfigTests(unittest.TestCase):
    def test_ephemeral_defaults_to_false(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(ephemeral_from_environment())

    def test_ephemeral_accepts_truthy_values(self):
        for value in ("1", "true", "TRUE", " yes ", "on"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"CODEX_EPHEMERAL": value}, clear=True):
                    self.assertTrue(ephemeral_from_environment())

    def test_ephemeral_rejects_other_values(self):
        for value in ("0", "false", "off", "unexpected"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"CODEX_EPHEMERAL": value}, clear=True):
                    self.assertFalse(ephemeral_from_environment())


if __name__ == "__main__":
    unittest.main()
