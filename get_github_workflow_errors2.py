# The error was:
# [FAILURE] File: .github, Line: 214
# Message: Process completed with exit code 1.

# Is there another `.github` directory?
import os
def find_github_dirs():
    for root, dirs, files in os.walk('.'):
        for d in dirs:
            if d == '.github':
                print(os.path.join(root, d))
find_github_dirs()
