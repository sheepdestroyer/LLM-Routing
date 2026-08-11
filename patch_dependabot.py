import re

with open('.github/dependabot.yml', 'r') as f:
    content = f.read()

# Dependabot fails to update pod.yaml if it contains docker.io/ prefix for some images
# that it internalizes without the prefix. Let's make sure pod.yaml doesn't have it either.
