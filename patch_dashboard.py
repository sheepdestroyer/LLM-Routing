with open("router/templates/dashboard.html", "r") as f:
    content = f.read()

content = content.replace(
    ".btn:focus-visible, #visualizer-link:focus-visible {\n            outline: 2px solid #818cf8;\n            outline-offset: 2px;\n            background: rgba(255, 255, 255, 0.06);\n            border-color: rgba(129, 140, 248, 0.5);\n            transform: translateX(4px);\n        }",
    "#visualizer-link:hover, .btn:focus-visible, #visualizer-link:focus-visible {\n            outline: 2px solid #818cf8;\n            outline-offset: 2px;\n            background: rgba(255, 255, 255, 0.06);\n            border-color: rgba(129, 140, 248, 0.5);\n            transform: translateX(4px);\n        }"
)

with open("router/templates/dashboard.html", "w") as f:
    f.write(content)
