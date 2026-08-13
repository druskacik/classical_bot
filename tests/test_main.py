import unittest
from unittest.mock import patch

import main


class MainTests(unittest.TestCase):
    def test_default_entrypoint_runs_combined_service(self):
        with (
            patch.object(main.sys, "argv", ["main.py"]),
            patch("automation.run_classical_bot_service.main") as service_main,
        ):
            main.main()
        service_main.assert_called_once_with()

    def test_arguments_are_rejected(self):
        with patch.object(main.sys, "argv", ["main.py", "--scheduler-only"]):
            with self.assertRaisesRegex(SystemExit, "Unknown arguments"):
                main.main()


if __name__ == "__main__":
    unittest.main()
