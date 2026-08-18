import ast

with open('router/main.py', 'r') as f:
    tree = ast.parse(f.read())
print("Syntax OK")
