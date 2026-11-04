import React from 'react';

// Epic 8, E8-1.1b: the confirmation shown before a rewind executes.
// Rewind is state-only (grill Q2): the conversation and agent state
// roll back to the chosen manifest point, but files the task wrote
// stay exactly as they are — so every file written after the point
// is enumerated here and labelled, in as many words, as one that
// "will NOT be reverted". Nothing is sent to the engine until the
// user confirms; App owns the request and the undo window that
// follows.
export default function RewindConfirmModal({ entry, filesNotReverted = [], onConfirm, onCancel, queued = false }) {
  if (!entry) return null;
  const files = Array.isArray(filesNotReverted) ? filesNotReverted : [];
  return (
    <div className="rewind-confirm-overlay" data-testid="rewind-confirm-modal">
      <div className="rewind-confirm-dialog" role="dialog" aria-modal="true" aria-label="Confirm rewind">
        <h3 className="rewind-confirm-title">Rewind to “{entry.label}”?</h3>
        <p className="rewind-confirm-body">
          The conversation and agent state will roll back to this point.
          Steps after it are held for a 5-second undo window, then deleted.
        </p>
        {files.length > 0 ? (
          <>
            <p className="rewind-confirm-body">
              These files were written after this point and{' '}
              <strong>will NOT be reverted</strong>:
            </p>
            <ul className="rewind-confirm-files" data-testid="rewind-files-not-reverted">
              {files.map((path) => (
                <li key={path}>{path}</li>
              ))}
            </ul>
          </>
        ) : (
          <p className="rewind-confirm-body">No files were written after this point.</p>
        )}
        {queued ? (
          <>
            {/* Wave 2 slice 2 (RUX-A-03): the rewind was confirmed
                while the engine was unreachable, so it sits in the
                offline queue. The modal stays open and says exactly
                that — it must not close as if the rewind had already
                happened. The request is committed to the queue, so
                the only action left is acknowledgement; App closes
                the modal when the flushed request lands. */}
            <p className="rewind-confirm-body" data-testid="rewind-queued-note">
              Queued — the engine is unreachable, so this rewind isn’t done yet.
              It will be sent as soon as the connection returns.
            </p>
            <div className="rewind-confirm-actions">
              <button
                type="button"
                className="rewind-cancel-btn"
                data-testid="rewind-queued-ok-btn"
                onClick={onCancel}
              >
                OK
              </button>
            </div>
          </>
        ) : (
          <div className="rewind-confirm-actions">
            <button
              type="button"
              className="rewind-confirm-btn"
              data-testid="rewind-confirm-btn"
              onClick={onConfirm}
            >
              Rewind
            </button>
            <button
              type="button"
              className="rewind-cancel-btn"
              data-testid="rewind-cancel-btn"
              onClick={onCancel}
            >
              Cancel
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
