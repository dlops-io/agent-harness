"""Run offline tests; show captured output on failures.

Use ``python -m tests --show-logs`` to also see passing tests' SDK diagnostics,
including deliberate policy/audit failures. Other unittest options still apply.
"""
from pathlib import Path
import sys
import unittest


def main():
    test_dir = Path(__file__).resolve().parent
    tutorial_dir = test_dir.parent
    # Make the tutorial package and root-level act modules available from either directory.
    # This affects only the test process; the working directory stays unchanged.
    sys.path.insert(0, str(tutorial_dir))
    arguments = sys.argv[1:]
    show_logs = "--show-logs" in arguments
    arguments = [arg for arg in arguments if arg != "--show-logs"]
    # Negative tests deliberately provoke SDK errors. Keep passing runs readable;
    # unittest restores captured stdout/stderr when a test fails or errors.
    unittest.main(module=None, buffer=not show_logs,
                  argv=[sys.argv[0], "discover", "-s", str(test_dir),
                        "-t", str(tutorial_dir), *arguments])


if __name__ == "__main__":
    main()
