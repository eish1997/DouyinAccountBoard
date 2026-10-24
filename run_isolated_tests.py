"""Run only explicitly selected, reviewed test modules in a fresh synthetic root."""
import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
import unittest
from test_isolation import create_root, install


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('modules', nargs='+')
    args = parser.parse_args()
    # No discovery: historical tests may use customer-derived fixtures.
    allowed = {'test_release_isolation', 'test_release_shared', 'test_release_package', 'test_release_collection', 'test_release_core', 'test_release_backup'}
    if not set(args.modules) <= allowed:
        parser.error('Test module has not been reviewed for synthetic execution')
    root = create_root(args.root)
    install(root)
    started = datetime.now(timezone.utc).isoformat()
    suite = unittest.defaultTestLoader.loadTestsFromNames(args.modules)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {'started_utc': started, 'ended_utc': datetime.now(timezone.utc).isoformat(),
              'modules': args.modules, 'tests': result.testsRun,
              'failures': len(result.failures), 'errors': len(result.errors),
              'skipped': len(result.skipped), 'exit_code': 0 if result.wasSuccessful() else 1,
              'synthetic_seed': 'synth-pc-1/synth-account-1/v1',
              'metrics': {name: getattr(sys.modules[name], 'METRICS') for name in args.modules
                          if hasattr(sys.modules.get(name), 'METRICS')},
              'source_sha256': {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                for path in Path(__file__).parent.glob('*.py')}}
    (root / 'test-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
