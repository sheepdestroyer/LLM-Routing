import re

with open("router/templates/dashboard.html", "r") as f:
    content = f.read()

# Add hover state for #visualizer-link to match .btn:hover
search_str = """        .btn:hover {
            background: rgba(255, 255, 255, 0.06);
            border-color: rgba(129, 140, 248, 0.3);
            transform: translateX(4px);
        }"""
replace_str = """        .btn:hover, #visualizer-link:hover {
            background: rgba(255, 255, 255, 0.06);
            border-color: rgba(129, 140, 248, 0.3);
            transform: translateX(4px);
        }"""
content = content.replace(search_str, replace_str)

# Ensure #visualizer-link has display: inline-block for transform to work
search_str2 = """        #visualizer-link:focus-visible {
            outline-offset: 4px;
            border-radius: 2px;
        }"""
replace_str2 = """        #visualizer-link {
            display: inline-block;
            transition: all 0.3s ease;
        }

        #visualizer-link:focus-visible {
            outline-offset: 4px;
            border-radius: 2px;
        }"""
content = content.replace(search_str2, replace_str2)

with open("router/templates/dashboard.html", "w") as f:
    f.write(content)
