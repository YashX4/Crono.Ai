"""One-command runner for every automated fake_sandbox/test_*.py file. Subprocess-runs
each one (each needs to be its own process — see test_helpers.py's module-docstring on
why env vars must be set before any timeblock_agent import), prints PASS/FAIL per file
plus its trace-log path, dumps a failing test's log tail to stdout, non-zero exit if
anything failed.

Cost note: this makes on the order of 25-35 real Claude Haiku API calls across the full
suite (day-start plans, classifications, incremental replans across repeated check-in
rounds, weekly review, including bounded-retry attempts) — cheap per-call but not free.
An on-demand regression check, not something to loop or run on every file save.

Usage: .venv/bin/python fake_sandbox/run_all_tests.py
"""

import subprocess
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_PYTHON = _DIR.parent / ".venv" / "bin" / "python"


def _tail(path: Path, n: int = 15) -> str:
    if not path.exists():
        return "(no trace log written)"
    lines = path.read_text().splitlines()
    return "\n".join(lines[-n:])


def main():
    # test_helpers.py is the shared library every test file imports, not a test itself —
    # running it directly just imports it with no assertions, a trivial false-positive PASS.
    test_files = sorted(f for f in _DIR.glob("test_*.py") if f.name != "test_helpers.py")
    if not test_files:
        print("No test_*.py files found in fake_sandbox/.")
        return 1

    results = []
    for f in test_files:
        print(f"=== {f.name} ===")
        proc = subprocess.run([str(_PYTHON), str(f)], cwd=str(_DIR.parent))
        results.append((f, proc.returncode))
        print()

    print("=== Summary ===")
    failed = []
    for f, rc in results:
        log_path = _DIR / "test_logs" / f"{f.stem}.log"
        status = "PASS" if rc == 0 else "FAIL"
        print(f"  {status}  {f.name}  (log: {log_path})")
        if rc != 0:
            failed.append((f, log_path))

    if failed:
        print(f"\n{len(failed)} of {len(results)} test file(s) failed.\n")
        for f, log_path in failed:
            print(f"--- tail of {log_path} ---")
            print(_tail(log_path))
            print()
        return 1

    print(f"\nAll {len(results)} test file(s) passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
