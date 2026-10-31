import React, { useEffect, useRef, useState } from 'react';

// Epic 8, E8-1.1b: the 5-second undo toast (grill Q3). After a
// rewind, the discarded steps are held by the engine for exactly
// this window; the toast counts it down and offers the one-click
// restore. When the countdown reaches zero the toast reports
// expiry once (App clears its state) and renders nothing — the
// engine prunes on its own timer regardless, and sending a new
// message in the session dismisses the toast even sooner (App
// clears it in handleSendMessage; the engine commits on START_TASK).
export default function UndoToast({ expiresAt, onUndo, onExpire }) {
  const [remaining, setRemaining] = useState(() =>
    Math.max(0, Math.ceil((expiresAt - Date.now()) / 1000)));
  const expiredRef = useRef(false);
  const onExpireRef = useRef(onExpire);
  useEffect(() => { onExpireRef.current = onExpire; }, [onExpire]);

  useEffect(() => {
    expiredRef.current = false;
    const tick = () => {
      const left = Math.ceil((expiresAt - Date.now()) / 1000);
      setRemaining(Math.max(0, left));
      if (left <= 0 && !expiredRef.current) {
        expiredRef.current = true;
        if (onExpireRef.current) onExpireRef.current();
      }
    };
    tick();
    const timer = setInterval(tick, 200);
    return () => clearInterval(timer);
  }, [expiresAt]);

  if (remaining <= 0) return null;
  return (
    <div className="rewind-undo-toast" data-testid="rewind-undo-toast" role="status">
      <span>Rewound. The discarded steps will be deleted in {remaining}s.</span>
      <button type="button" data-testid="rewind-undo-btn" onClick={onUndo}>
        Undo
      </button>
    </div>
  );
}
