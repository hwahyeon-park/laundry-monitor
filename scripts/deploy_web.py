"""Deploy both pages together and select the default display language."""
import argparse
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--language', choices=['kor', 'eng'], required=True)
    parser.add_argument('--settings-only', action='store_true',
                        help='Switch the default language after the bilingual app is deployed.')
    parser.add_argument('--app', default='laundry-monitor')
    parser.add_argument('--resource-group', default='laundry')
    parser.add_argument('--dry-run', action='store_true', help='Show commands without changing files or Azure.')
    args = parser.parse_args(argv)
    commands = []
    if not args.settings_only:
        commands += [
            [sys.executable, str(ROOT / 'scripts/package_web.py')],
            ['az', 'webapp', 'deploy', '--resource-group', args.resource_group,
             '--name', args.app, '--type', 'zip', '--src-path', str(ROOT / 'dist/laundry-web.zip'),
             '--only-show-errors', '--output', 'none'],
        ]
    commands.append(['az', 'webapp', 'config', 'appsettings', 'set',
                     '--resource-group', args.resource_group, '--name', args.app,
                     '--settings', f'UI_LANGUAGE={args.language}', '--only-show-errors', '--output', 'none'])
    if args.dry_run:
        for command in commands:
            print(shlex.join(command))
        return 0
    try:
        for index, command in enumerate(commands, 1):
            print(f'Step {index}/{len(commands)}: ' + ('Select default language' if index == len(commands)
                  else 'Build package' if command[0] != 'az' else 'Deploy bilingual package'), flush=True)
            subprocess.run(command, cwd=ROOT, check=True, timeout=1200)
    except FileNotFoundError:
        print('Azure CLI was not found. Install Azure CLI and sign in with az login.', file=sys.stderr)
        return 1
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print('Deployment did not complete. Check Azure status before retrying. '
              'A completed ZIP deployment may already be active.', file=sys.stderr)
        return 1
    print(f'Default language set to {args.language}. Both pages are available using ?lang=kor or ?lang=eng.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
