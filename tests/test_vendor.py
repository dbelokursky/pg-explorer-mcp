import os
import shutil
import sys
import tempfile
import unittest

import support

sys.path.insert(0, os.path.join(support.ROOT, "scripts"))
import check_vendor  # noqa: E402


class VendorIntegrityTests(unittest.TestCase):
    def test_vendored_files_match_the_wheels_they_came_from(self):
        self.assertEqual(check_vendor.problems(), [])

    def test_an_edited_file_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            copy = os.path.join(directory, "vendor")
            shutil.copytree(check_vendor.VENDOR, copy, ignore=shutil.ignore_patterns("__pycache__"))
            with open(os.path.join(copy, "six.py"), "a", encoding="utf-8") as handle:
                handle.write("\n# edited by hand\n")
            os.remove(os.path.join(copy, "scramp", "core.py"))
            self.assertEqual(check_vendor.problems(copy), ["missing: scramp/core.py", "changed: six.py"])


if __name__ == "__main__":
    unittest.main()
