"""Build the checked-in widget template; Node/Terser are development tools only."""
from pathlib import Path
import argparse
import subprocess


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    folder=root/'assets/widgets/farming'
    prefix,body=(folder/'widget.source.html').read_text().split('<script>',1)
    script,suffix=body.split('</script>',1)
    result=subprocess.run(['npx','--yes','--package=terser@5.51.2','--','terser',
        '--ecma','2020','--compress','passes=3,toplevel=true',
        '--mangle','toplevel,reserved=[A,D,pick]'],input=script,text=True,
        capture_output=True,check=True,timeout=90)
    built=prefix+'<script>'+result.stdout.strip()+'</script>'+suffix
    target=folder/'widget.html'
    if args.check:
        if not target.exists() or target.read_text()!=built:
            raise SystemExit('Widget template is stale; run python scripts/build_farming_widget.py')
        print('Widget template matches source')
    else:
        target.write_text(built)
        print('Built',target.relative_to(root))


if __name__=='__main__':
    main()
