## 2024-05-24 - Accessibility for Custom Interactive Elements
**Learning:** Custom interactive elements (like `div` tags used as copy-to-clipboard banners) lack semantic meaning and keyboard support out of the box, which makes them inaccessible to keyboard and screen reader users.
**Action:** When building custom interactive elements, always ensure they are accessible by adding `role="button"`, `tabindex="0"`, an appropriate `aria-label`, a keyboard event handler (e.g., `onkeydown` for Enter/Space), and a visible focus state (`:focus-visible`).
