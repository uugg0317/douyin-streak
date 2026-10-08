"""Run auth/bootstrap/storage regressions in a fresh process with synthetic data."""

from pathlib import Path
import subprocess
import sys
import unittest


class IsolatedAuthenticationRegression(unittest.TestCase):
    def test_auth_bootstrap_and_config_regressions(self):
        script = Path(__file__).with_name("_auth_regressions.py")
        result = subprocess.run([sys.executable, str(script)], capture_output=True,
                                text=True, encoding="utf-8", timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # Keep the child test count available in the normal test-discovery output.
        print(result.stderr.strip())


if __name__ == "__main__":
    unittest.main()
