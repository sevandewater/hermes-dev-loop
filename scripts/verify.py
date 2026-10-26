#!/usr/bin/env python3
"""Credential-free local/CI verification of units and shipped image builds."""
import argparse
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--units-only', action='store_true',
                        help='Partial gate: run units using the current interpreter; no builds')
    args = parser.parse_args(argv)
    if args.units_only:
        sys.path.insert(0, str(ROOT))
        return run_units(ROOT / 'tests')
    compose = ['docker', 'compose', '--project-directory', str(ROOT),
               '--file', str(ROOT / 'compose.yaml'), '--profile', 'github']
    build = subprocess.run(compose + ['build', 'hermes', 'llm-gateway', 'squid', 'github-broker'])
    if build.returncode:
        return build.returncode
    return subprocess.run([
        'docker', 'run', '--rm', '--network=none', '--read-only',
        '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
        '--tmpfs', '/tmp:rw,nosuid,nodev,size=536870912,mode=1777',
        '--env', 'HOME=/tmp', '--env', 'PYTHONDONTWRITEBYTECODE=1',
        '--mount', f'type=bind,src={ROOT},dst=/review,readonly',
        '--workdir', '/review', '--entrypoint', '/opt/hermes/.venv/bin/python',
        'hermes-dev-loop-hermes:local', '-W', 'error::ResourceWarning',
        '/review/scripts/verify.py', '--units-only',
    ]).returncode


def run_units(directory, *, stream=None):
    suite = unittest.TestLoader().discover(str(directory), pattern='test_*.py')
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return 0 if result.testsRun and result.wasSuccessful() and not result.skipped else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except OSError as error:
        print(f'Verification could not run: {error}. Docker Engine and Compose '
              '2.17+ with BuildKit are required for the full gate.', file=sys.stderr)
        raise SystemExit(1)
