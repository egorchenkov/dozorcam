"""Герметичный контракт моста (bridge-contract-test.sh) — частью общего прогона pytest."""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import unittest

SCRIPT = pathlib.Path(__file__).with_name("bridge-contract-test.sh")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("openssl") and shutil.which("curl"),
                     "нужны ffmpeg, openssl и curl")
class BridgeContractTest(unittest.TestCase):
    def test_contract_script_passes(self):
        env = {**os.environ, "CCTV_TEST_PYTHON": sys.executable}
        result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=600)
        self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:])
        self.assertIn("PASS:", result.stdout)


if __name__ == "__main__":
    unittest.main()
