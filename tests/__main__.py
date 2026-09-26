"""Run python -m tests here, or python -m formaggio-store.tests from the parent."""
from pathlib import Path
import sys
import unittest


def main():
    test_dir = Path(__file__).resolve().parent
    tutorial_dir = test_dir.parent
    # Make the tutorial package and root-level act modules available from either directory.
    # This affects only the test process; the working directory stays unchanged.
    sys.path.insert(0, str(tutorial_dir))
    unittest.main(module=None, argv=[sys.argv[0], "discover", "-s", str(test_dir),
                                     "-t", str(tutorial_dir), *sys.argv[1:]])


if __name__ == "__main__":
    main()
