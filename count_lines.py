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

total = 0
print("--- FILE LINE COUNTS ---")
for f in all_files:
    try:
        with open(f, 'r', encoding='utf-8') as file:
            lines = len(file.readlines())
            total += lines
            print(f"{f}: {lines}")
    except Exception as e:
        print(f"Error reading {f}: {e}")

print(f"--- TOTAL LINES: {total} ---")
