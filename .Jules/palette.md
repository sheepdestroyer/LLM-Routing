## 2024-05-24 - Interactive Div Accessibility
**Learning:** Custom interactive elements (like copy-to-clipboard banners) built with `div` tags lack native keyboard support and screen reader context in this application's templates.
**Action:** Always add `role="button"`, `tabindex="0"`, `aria-label`, and keyboard event handlers (`onkeydown` for Enter/Space) to custom interactive `div` elements to ensure accessibility.
