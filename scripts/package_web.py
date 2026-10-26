"""이 웹 프로젝트에서 App Service 배포 ZIP 생성. 외부 업데이트 폴더 불필요."""
import argparse
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

root = Path(__file__).resolve().parents[1]
names = ['app.py', 'realtime.py', 'history.py', 'notifications.py', 'table_state.py', 'local_settings.py', 'cycle_detector.py', 'requirements.txt', 'gunicorn.conf.py']
files = [root/name for name in names]
for folder in ('static', 'templates'):
    files.extend(p for p in (root/folder).rglob('*') if p.is_file())
for file in files:
    if not file.is_file() or file.is_symlink() or root not in file.resolve().parents:
        raise SystemExit('Missing or unsupported source: '+str(file))
    if any(part.startswith('.') for part in file.relative_to(root).parts):
        raise SystemExit('Hidden source file excluded: '+str(file))
parser = argparse.ArgumentParser(description='Package the current branch without local secrets.')
parser.add_argument('--output', default='dist/laundry-web.zip')
args = parser.parse_args()
target = root / args.output
target.parent.mkdir(exist_ok=True)
with ZipFile(target, 'w', ZIP_DEFLATED) as archive:
    for file in sorted(files):
        archive.write(file, file.relative_to(root))
print(target)
