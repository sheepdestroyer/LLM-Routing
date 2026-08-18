import re

with open("router/templates/dashboard.html", "r") as f:
    content = f.read()

print("HOVER SELECTORS:")
lines = content.split('\n')
for i, line in enumerate(lines):
    if ':hover' in line:
        print(f"{i+1}: {line}")
