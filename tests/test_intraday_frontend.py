"""Run the Intraday Desk behavior contract against an offline JavaScript VM."""

from pathlib import Path
import shutil
import subprocess
import unittest


class IntradayFrontendTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is needed for intraday behavior checks")
    def test_intraday_behavior(self):
        test = Path(__file__).with_name("test_intraday_behavior.cjs")
        result = subprocess.run([shutil.which("node"), str(test)], text=True,
                                encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
