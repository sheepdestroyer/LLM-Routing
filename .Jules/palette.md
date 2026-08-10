## 2024-08-10 - Keyboard accessibility for interactive div elements
**Learning:** When using custom `div` elements as clickable buttons for copying text (e.g., `.oauth-banner-cmd`), they are invisible to screen readers and keyboard navigation by default, leading to accessibility issues.
**Action:** Always ensure that custom interactive `div` or `span` elements include `role="button"`, `tabindex="0"`, a descriptive `aria-label`, and an `onkeydown` handler to capture 'Enter' and 'Space' keypresses to trigger the click action natively.
