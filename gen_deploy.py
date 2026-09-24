import base64, json, os

base = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sync_service')
files = {}
skip_dirs = {'__pycache__', 'tests', '.pytest_cache', 'data'}
skip_exts = {'.pyc', '.db'}
skip_files = {'.env.example', '.dockerignore', 'pytest.ini', 'favicon.jpg'}

for root, dirs, filenames in os.walk(base):
    dirs[:] = [d for d in dirs if d not in skip_dirs]
    for fn in filenames:
        if fn.endswith('.pyc') or os.path.splitext(fn)[1] in skip_exts or fn in skip_files:
            continue
        fp = os.path.join(root, fn)
        rel = os.path.relpath(fp, base).replace(os.sep, '/')
        with open(fp, 'rb') as f:
            files[rel] = base64.b64encode(f.read()).decode('ascii')

lines = ['#!/usr/bin/env python3']
lines.append('"""Deploy script: create all project files on server."""')
lines.append('import base64, os, sys')
lines.append('')
lines.append('FILES = {')
for path, b64 in sorted(files.items()):
    lines.append('    %s: "%s",' % (json.dumps(path), b64))
lines.append('}')
lines.append('')
lines.append('DEST = "/opt/sync-service"')
lines.append('os.makedirs(DEST, exist_ok=True)')
lines.append('os.makedirs(os.path.join(DEST, "data"), exist_ok=True)')
lines.append('os.makedirs(os.path.join(DEST, "app", "static"), exist_ok=True)')
lines.append('')
lines.append('for path, b64 in FILES.items():')
lines.append('    full = os.path.join(DEST, path)')
lines.append('    os.makedirs(os.path.dirname(full), exist_ok=True)')
lines.append('    with open(full, "wb") as f:')
lines.append('        f.write(base64.b64decode(b64))')
lines.append('    print("  created:", path)')
lines.append('')
lines.append('# Create empty favicon placeholder')
lines.append('fav = os.path.join(DEST, "app", "static", "favicon.jpg")')
lines.append('if not os.path.exists(fav):')
lines.append('    with open(fav, "wb") as f:')
lines.append('        f.write(b"")  # empty placeholder')
lines.append('    print("  created: app/static/favicon.jpg (placeholder)")')
lines.append('')
lines.append('print()')
lines.append('print("Total:", len(FILES), "files created")')
lines.append('print()')
lines.append('print("Now run:")')
lines.append('print("  cd /opt/sync-service")')
lines.append('print("  chmod 600 .env")')
lines.append('print("  docker compose up -d --build")')

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'deploy_generated.py')
with open(out, 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines))

print('Generated deploy_generated.py with %d files' % len(files))
print('File size: %d bytes (%d KB)' % (os.path.getsize(out), os.path.getsize(out) // 1024))
