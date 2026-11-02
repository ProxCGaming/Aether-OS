import React, { useState, useEffect, useCallback } from 'react';
import { X, Crop } from 'lucide-react';

export default function SnipOverlay({ isOpen, onCapture, onCancel, onError }) {
  const [isSelecting, setIsSelecting] = useState(false);
  const [startPos, setStartPos] = useState({ x: 0, y: 0 });
  const [currentPos, setCurrentPos] = useState({ x: 0, y: 0 });
  // N04: a failed capture is a visible state, never a silent
  // synthetic image. The overlay stays open so the user can retry.
  const [captureError, setCaptureError] = useState(null);

  // Escape key cancels capture
  useEffect(() => {
    if (!isOpen) return;
    const handleKeyDown = (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        setCaptureError(null);
        if (onCancel) onCancel();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, onCancel]);

  const handleMouseDown = useCallback((e) => {
    // Only left click starts selection
    if (e.button !== 0) return;
    setCaptureError(null);
    setIsSelecting(true);
    setStartPos({ x: e.clientX, y: e.clientY });
    setCurrentPos({ x: e.clientX, y: e.clientY });
  }, []);

  const handleMouseMove = useCallback((e) => {
    if (!isSelecting) return;
    setCurrentPos({ x: e.clientX, y: e.clientY });
  }, [isSelecting]);

  const handleMouseUp = useCallback(async () => {
    if (!isSelecting) return;
    setIsSelecting(false);

    const x = Math.min(startPos.x, currentPos.x);
    const y = Math.min(startPos.y, currentPos.y);
    const width = Math.abs(currentPos.x - startPos.x);
    const height = Math.abs(currentPos.y - startPos.y);

    // Minimum region threshold (5px)
    if (width < 5 || height < 5) {
      return;
    }

    const bounds = { x, y, width, height };

    // N04: the only honest outcomes are a real capture or a visible
    // failure. No capture seam, a rejected capture, or an empty
    // result all land in the same place: tell the user, keep the
    // overlay open for a retry, and never call onCapture.
    const failCapture = (message, err) => {
      setCaptureError(message);
      if (onError) onError(err || new Error(message));
    };

    if (!window.electronAPI || typeof window.electronAPI.captureScreen !== 'function') {
      failCapture('Screen capture is not available in this window. Press ESC to cancel.');
      return;
    }

    let dataUrl = null;
    try {
      const res = await window.electronAPI.captureScreen(bounds);
      if (res && typeof res === 'string') {
        dataUrl = res;
      } else if (res && res.data) {
        dataUrl = res.data;
      }
    } catch (err) {
      console.error('Electron captureScreen error:', err);
      failCapture('Screen capture failed. Drag to try again, or press ESC to cancel.', err);
      return;
    }

    if (!dataUrl || typeof dataUrl !== 'string' || !dataUrl.startsWith('data:image/')) {
      failCapture('Screen capture failed. Drag to try again, or press ESC to cancel.');
      return;
    }

    setCaptureError(null);
    if (onCapture) {
      onCapture({
        name: `snip-${Date.now()}.png`,
        mime_type: 'image/png',
        data: dataUrl,
        bounds,
      });
    }
  }, [isSelecting, startPos, currentPos, onCapture, onError]);

  if (!isOpen) return null;

  const left = Math.min(startPos.x, currentPos.x);
  const top = Math.min(startPos.y, currentPos.y);
  const width = Math.abs(currentPos.x - startPos.x);
  const height = Math.abs(currentPos.y - startPos.y);

  return (
    <div
      data-testid="snip-overlay"
      onMouseDown={handleMouseDown}
      onMouseMove={handleMouseMove}
      onMouseUp={handleMouseUp}
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 10000,
        background: 'rgba(0, 0, 0, 0.4)',
        cursor: 'crosshair',
        userSelect: 'none',
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      {/* Top Banner Toolbar */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '12px 20px',
          background: 'rgba(15, 15, 25, 0.85)',
          backdropFilter: 'blur(12px)',
          borderBottom: '1px solid rgba(255, 255, 255, 0.1)',
          color: '#f0f0f5',
          pointerEvents: 'auto',
        }}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <Crop size={18} color="#a78bfa" />
          <span style={{ fontSize: '13px', fontWeight: 500 }}>
            Drag to select screen region &bull; ESC to cancel
          </span>
        </div>
        <button
          type="button"
          data-testid="snip-cancel-btn"
          onClick={() => { setCaptureError(null); if (onCancel) onCancel(); }}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '6px',
            background: 'rgba(255, 255, 255, 0.08)',
            border: '1px solid rgba(255, 255, 255, 0.15)',
            color: '#cbd5e1',
            borderRadius: '8px',
            padding: '6px 12px',
            cursor: 'pointer',
            fontSize: '12px',
            fontWeight: 500,
          }}
        >
          <X size={14} />
          Cancel
        </button>
      </div>

      {/* N04: honest capture failure — visible, and the overlay
          stays open so the user can drag again to retry. */}
      {captureError && (
        <div
          data-testid="snip-error"
          role="alert"
          style={{
            margin: '12px auto 0',
            background: 'rgba(127, 29, 29, 0.92)',
            border: '1px solid rgba(248, 113, 113, 0.55)',
            color: '#fecaca',
            fontSize: '12.5px',
            fontWeight: 500,
            padding: '8px 14px',
            borderRadius: '8px',
            pointerEvents: 'none',
            maxWidth: '520px',
            textAlign: 'center',
          }}
        >
          {captureError}
        </div>
      )}

      {/* Render Active Selection Rectangle */}
      {isSelecting && (width > 0 || height > 0) && (
        <div
          data-testid="snip-selection-box"
          style={{
            position: 'absolute',
            left: `${left}px`,
            top: `${top}px`,
            width: `${width}px`,
            height: `${height}px`,
            border: '2px solid #8b5cf6',
            background: 'rgba(139, 92, 246, 0.15)',
            boxShadow: '0 0 0 9999px rgba(0, 0, 0, 0.35)',
            pointerEvents: 'none',
          }}
        >
          {width > 40 && height > 24 && (
            <div
              style={{
                position: 'absolute',
                bottom: '-24px',
                left: '0px',
                background: '#8b5cf6',
                color: '#fff',
                fontSize: '11px',
                fontFamily: 'monospace',
                padding: '2px 6px',
                borderRadius: '4px',
                whiteSpace: 'nowrap',
              }}
            >
              {Math.round(width)} &times; {Math.round(height)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
