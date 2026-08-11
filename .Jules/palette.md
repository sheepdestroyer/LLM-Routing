## 2024-08-10 - Keyboard accessibility for interactive elements
**Learning:** Using custom `div` elements for interactive actions creates non-semantic markup, requires manual `tabindex` / `onkeydown` handlers, and lacks screen-reader feedback.
**Action:** Replace interactive custom `div` or `span` elements with native `<button type="button">` elements, and ensure dynamic status tooltips include `role="status"` and `aria-live="polite"`.
