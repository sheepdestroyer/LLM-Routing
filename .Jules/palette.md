## 2024-08-10 - Keyboard accessibility for interactive elements
**Learning:** Using custom `div` elements for interactive actions creates non-semantic markup, requires manual `tabindex` / `onkeydown` handlers, and lacks screen-reader feedback.
**Action:** Replace interactive custom `div` or `span` elements with native `<button type="button">` elements, and ensure dynamic status tooltips include `role="status"` and `aria-live="polite"`.

## 2026-08-12 - Accessible External Link Context & Focus Indicators
**Learning:** Using `aria-label` on links that already contain visible text overrides child DOM nodes for screen readers. Using `aria-describedby` with visually hidden text (`.visually-hidden`) preserves the link text while adding new-tab context.
**Action:** Always add `aria-hidden="true"` to decorative icons/arrows, `rel="noopener noreferrer"` to external links, `aria-describedby` for new-tab context on links with visible text, and define `:focus-visible` styles for interactive elements.

## 2026-08-12 - Visible keyboard focus indicators (:focus-visible)
**Learning:** Keyboard navigation via Tab key requires clear visual focus indicators on interactive elements (`.btn`, `#visualizer-link`, `.oauth-banner-cmd`) to satisfy WCAG SC 2.4.7 (Focus Visible) without cluttering mouse click UI state.
**Action:** Apply `:focus-visible` styling with strong contrast outlines (`2px solid #818cf8`) and appropriate `outline-offset` across all interactive dashboard links and buttons.

## 2026-08-13 - Accessible Text Truncation & Hover Tooltips
**Learning:** Heavily truncated text elements using CSS `text-overflow: ellipsis` (like long model IDs or descriptions) in dashboards can obscure content from visual users. Adding a `title` attribute provides a native hover tooltip for visual inspection. Do NOT add `tabindex="0"` or `role="button"` to non-interactive text elements, as screen readers naturally access the full DOM text string without extra tab stops or fake button semantics.
**Action:** When truncating important information visually, apply a `title` attribute containing the full text to expose visual hover tooltips. Do not make non-interactive text focusable or assign button roles. Ensure JavaScript updating dynamic fields syncs the `.title` property.

## 2026-08-14 - Keyboard Focus Parity for Hover Animations
**Learning:** When interactive elements (like links or buttons) have child elements that animate on `:hover` (e.g., `.btn-arrow` translating horizontally), those child elements must also animate on `:focus-visible`. Otherwise, keyboard users miss out on visual interaction cues provided to mouse users.
**Action:** When defining `:hover` state animations for child elements, always pair it with the corresponding `:focus-visible` selector on the parent (e.g., `.btn:hover .btn-arrow, .btn:focus-visible .btn-arrow`).

## 2026-08-16 - Focus Parity for CSS Transform Animations
**Learning:** When interactive elements (like `.btn` links) have CSS `transform` animations on `:hover` (e.g., `transform: translateX(4px)`), missing the corresponding `transform` on the `:focus-visible` state means keyboard users do not get the same visual feedback as mouse users, creating an inconsistent and less polished experience.
**Action:** Ensure CSS `transform` and other layout-affecting animations applied on `:hover` are also applied on `:focus-visible` for the parent element to maintain focus parity.
## 2026-08-16 - Ensure layout animations apply to inline elements
**Learning:** `<a>` tags and other default `inline` elements cannot be visually animated with CSS `transform` (like `transform: translateX`) because the `transform` property is ignored on non-replaced inline boxes by the browser engine.
**Action:** Always add `display: inline-block;` (or `block`) to interactive inline elements when applying layout animations (like hover or focus states) to ensure the animation actually renders.
