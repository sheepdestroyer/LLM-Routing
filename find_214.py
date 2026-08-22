import os

for root, dirs, files in os.walk('.'):
    for name in files:
        path = os.path.join(root, name)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                if len(lines) >= 214:
                    pass
        except:
            pass
