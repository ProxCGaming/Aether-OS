import React, { useRef } from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { useFocusTrap } from '../useFocusTrap';

// S-9 (FF1-07): focus stays inside an active trap and returns to
// the invoker when the trap releases.
function TrapBox({ active }) {
  const ref = useRef(null);
  useFocusTrap(ref, active);
  return (
    <div ref={ref} role="dialog" aria-modal="true" data-testid="trap">
      <button>First</button>
      <button>Second</button>
    </div>
  );
}

describe('useFocusTrap (S-9)', () => {
  test('focus moves into the trap on engage and Tab wraps at both ends', () => {
    render(
      <>
        <button>Outside</button>
        <TrapBox active />
      </>
    );
    const first = screen.getByText('First');
    const second = screen.getByText('Second');
    // Initial focus lands on the first focusable inside the trap.
    expect(document.activeElement).toBe(first);
    second.focus();
    fireEvent.keyDown(document, { key: 'Tab' });
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(second);
  });

  test('focus is restored to the invoker when the trap releases', () => {
    // The invoker lives in a tree that OUTLIVES the modal — that is
    // the real shape (a modal unmounts; its invoker stays).
    function Harness() {
      const [open, setOpen] = React.useState(false);
      return (
        <>
          <button onClick={() => setOpen(true)}>Outside</button>
          {open && <TrapBox active />}
          <button data-testid="dismiss" onClick={() => setOpen(false)}>Dismiss</button>
        </>
      );
    }
    render(<Harness />);
    const outside = screen.getByText('Outside');
    outside.focus();
    fireEvent.click(outside);
    expect(document.activeElement).toBe(screen.getByText('First'));
    fireEvent.click(screen.getByTestId('dismiss'));
    expect(screen.queryByTestId('trap')).not.toBeInTheDocument();
    expect(document.activeElement).toBe(outside);
  });

  test('an inactive trap never moves focus', () => {
    render(
      <>
        <button>Outside</button>
        <TrapBox active={false} />
      </>
    );
    const outside = screen.getByText('Outside');
    outside.focus();
    fireEvent.keyDown(document, { key: 'Tab' });
    expect(document.activeElement).toBe(outside);
  });
});
