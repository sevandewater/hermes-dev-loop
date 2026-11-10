#!/usr/bin/env python3
"""Host-only isolated network canary in running Squid; no rebuild or policy changes."""
import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    command = ['docker', 'compose', '--project-directory', str(ROOT),
               '-f', str(ROOT / 'compose.yaml'), 'exec', '-T', 'squid', 'python3', '-']
    try:
        subprocess.run(command, input=(ROOT / 'tests/squid_broker_canary.py').read_text(),
                       text=True, stderr=subprocess.DEVNULL, timeout=60, check=True)
    except (OSError, subprocess.SubprocessError) as error:
        category = ('command-deadline' if isinstance(error, subprocess.TimeoutExpired)
                    else 'command-unavailable' if isinstance(error, OSError)
                    else 'command-failed')
        print(f'FAIL ISOLATED {category}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
