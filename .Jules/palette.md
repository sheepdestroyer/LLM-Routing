## 2026-08-12 - Accessible External Link Context & Focus Indicators
**Learning:** Using `aria-label` on links that already contain visible text overrides child DOM nodes for screen readers. Using `aria-describedby` with visually hidden text (`.visually-hidden`) preserves the link text while adding new-tab context.
**Action:** Always add `aria-hidden="true"` to decorative icons/arrows, `rel="noopener noreferrer"` to external links, `aria-describedby` for new-tab context on links with visible text, and define `:focus-visible` styles for interactive elements.
