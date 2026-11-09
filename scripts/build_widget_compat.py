"""Build the shared numeric bootstrap; Node/Terser are development tools only."""
from pathlib import Path
import argparse
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    folder = Path(__file__).resolve().parents[1] / 'assets/widgets'
    result = subprocess.run(
        ['npx', '--yes', '--package=terser@5.51.2', '--', 'terser',
         '--ecma', '2020', '--compress', 'passes=3,toplevel=true',
         '--mangle', 'toplevel'],
        input=(folder / 'numeric-compat.js').read_text(), text=True,
        capture_output=True, check=True, timeout=90)
    built = result.stdout.strip() + '\n'
    target = folder / 'numeric-compat.min.js'
    if args.check:
        if not target.exists() or target.read_text() != built:
            raise SystemExit('Numeric bootstrap is stale; run python scripts/build_widget_compat.py')
        print('Numeric bootstrap matches source')
    else:
        target.write_text(built)
        print('Built assets/widgets/numeric-compat.min.js')


if __name__ == '__main__':
    main()
