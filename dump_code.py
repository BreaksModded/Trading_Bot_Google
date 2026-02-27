import os

dirs = ['core', 'config', 'data', 'exchange', 'utils', 'tests', 'api', 'backtesting', 'services']
root_files = ['main.py', 'run.py', '.env.example', 'requirements.txt', 'pyproject.toml']
all_files = []

for d in dirs:
    if os.path.isdir(d):
        for root, _, files in os.walk(d):
            if '__pycache__' in root: continue
            for f in files:
                if f.endswith('.py') or f.endswith('.sql'):
                    all_files.append(os.path.join(root, f))

for f in root_files:
    if os.path.exists(f):
        all_files.append(f)

all_files = sorted(set(all_files))

with open('all_code.txt', 'w', encoding='utf-8') as out:
    for f in all_files:
        try:
            with open(f, 'r', encoding='utf-8') as file:
                content = file.read()
                out.write(f"\n\n{'='*80}\n")
                out.write(f"FILE: {f}\n")
                out.write(f"{'='*80}\n\n")
                out.write(content)
        except Exception as e:
            out.write(f"\nError reading {f}: {e}\n")
