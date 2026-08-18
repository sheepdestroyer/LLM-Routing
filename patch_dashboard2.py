with open("router/templates/dashboard.html", "r") as f:
    content = f.read()

content = content.replace(
    "#visualizer-link:focus-visible {\n            outline-offset: 4px;\n            border-radius: 2px;\n        }",
    "#visualizer-link:hover, #visualizer-link:focus-visible {\n            outline-offset: 4px;\n            border-radius: 2px;\n        }"
)

with open("router/templates/dashboard.html", "w") as f:
    f.write(content)
