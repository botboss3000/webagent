"""Fix print() calls in test_temp_session_dbs.py to use safe_print()"""
import re

with open('temp/test_temp_session_dbs.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Replace all remaining 'print(...' that are not inside def or part of safe_print
# Strategy: find lines starting with optional spaces then 'print(' and replace with 'safe_print('

lines = content.split('\n')
fixed = 0
for i, line in enumerate(lines):
    stripped = line.strip()
    # Match lines that start a print() call (not inside a string or comment)
    # but skip the def log and def safe_print lines
    if stripped.startswith('print(') and 'def print' not in line and 'safe_print' not in line:
        indent = line[:len(line) - len(line.lstrip())]
        lines[i] = indent + 'safe_print(' + stripped[6:]
        fixed += 1

content = '\n'.join(lines)
with open('temp/test_temp_session_dbs.py', 'w', encoding='utf-8') as f:
    f.write(content)
print(f'Fixed {fixed} lines')
