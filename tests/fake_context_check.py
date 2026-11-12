#!/usr/bin/env python3
"""Context-check output fixture, restricted to the fake ACP executable."""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--binary', required=True)
parser.add_argument('--dry-run', action='store_true', required=True)
parser.add_argument('--no-env-fix', action='store_true', required=True)
args = parser.parse_args()
if Path(args.binary).name != 'fake_agy.py':
    raise SystemExit('This checker is only for tests/fake_agy.py')
for gate in ('1', '2', '3A', '3B', '4A', '4B'):
    print(f'[=] [Gate {gate}] test fixture: already patched')
print('[=] all gates already unlocked; nothing to write')
