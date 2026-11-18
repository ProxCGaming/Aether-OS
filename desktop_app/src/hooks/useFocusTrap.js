import { useEffect } from 'react';

// Full Furnish S-9 (FF1-07): the shared modal focus pattern.
// While `active`, Tab / Shift+Tab cycle inside the container
// referenced by `ref` — focus never reaches the background UI —
// and when the trap deactivates (or unmounts) focus returns to
// whatever element had it before the trap engaged. On engage, the
// first focusable element inside the container takes focus (or
// the container itself when it has none / `initialFocusRef` is
// not supplied).
//
// The caller owns the semantics: the container element carries
// role="dialog" aria-modal="true" (and aria-label/labelledby);
// combobox-style surfaces (the command palette) layer their
// listbox/option roles + aria-activedescendant on top. Escape
// handling stays with the caller — what Esc means (close, step
// back) is the surface's decision, not the trap's.
export const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(', ');

function focusablesIn(container) {
  if (!container) return [];
  // No layout-based visibility filter: jsdom has no layout
  // (offsetParent is always null) and these surfaces render their
  // controls conditionally anyway, so DOM presence is the honest
  // criterion here. Explicitly-hidden elements are still skipped.
  return Array.from(container.querySelectorAll(FOCUSABLE_SELECTOR))
    .filter((el) => !el.hidden && el.type !== 'hidden');
}

export function useFocusTrap(ref, active, initialFocusRef = null) {
  useEffect(() => {
    if (!active) return undefined;
    const container = ref.current;
    if (!container) return undefined;

    const previouslyFocused = document.activeElement;

    const initial = (initialFocusRef && initialFocusRef.current)
      || focusablesIn(container)[0]
      || container;
    // Effects run after the DOM commit, so the focusables exist.
    if (initial && typeof initial.focus === 'function') initial.focus();

    const onKeyDown = (event) => {
      if (event.key !== 'Tab') return;
      const items = focusablesIn(container);
      if (items.length === 0) {
        event.preventDefault();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown, true);

    return () => {
      document.removeEventListener('keydown', onKeyDown, true);
      if (previouslyFocused && typeof previouslyFocused.focus === 'function'
        && document.contains(previouslyFocused)) {
        previouslyFocused.focus();
      }
    };
  }, [ref, active, initialFocusRef]);
}

export default useFocusTrap;
