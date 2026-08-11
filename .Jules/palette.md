## 2026-08-11 - Accessibility on Interactive Divs
**Learning:** Found a pattern where custom interactive elements (like the copy-to-clipboard banners) were built using `div` tags with inline `onclick` handlers, completely ignoring keyboard accessibility (no focus states, unable to trigger via Enter/Space, missing ARIA).
**Action:** When creating or modifying custom interactive `div` elements, always apply `role="button"`, `tabindex="0"`, a descriptive `aria-label`, an `onkeydown` handler to allow keyboard triggering (`this.click()`), and `:focus-visible` styles for visual feedback.
