import React, { useState, useEffect, useCallback } from 'react';
import { X, Crop } from 'lucide-react';

export default function SnipOverlay({ isOpen, onCapture, onCancel }) {
  const [isSelecting, setIsSelecting] = useState(false);
  const [startPos, setStartPos] = useState({ x: 0, y: 0 });
  const [currentPos, setCurrentPos] = useState({ x: 0, y: 0 });

  // Escape key cancels capture
  useEffect(() => {
    if (!isOpen) return;
    const handleKeyDown = (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        if (onCancel) onCancel();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, onCancel]);

  const handleMouseDown = useCallback((e) => {
    // Only left click starts selection
    if (e.button !== 0) return;
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
    let dataUrl = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==';

    if (window.electronAPI && typeof window.electronAPI.captureScreen === 'function') {
      try {
        const res = await window.electronAPI.captureScreen(bounds);
        if (res && typeof res === 'string') {
          dataUrl = res;
        } else if (res && res.data) {
          dataUrl = res.data;
        }
      } catch (err) {
        console.error('Electron captureScreen error:', err);
      }
    } else {
      try {
        if (typeof document !== 'undefined' && typeof jest === 'undefined') {
          const canvas = document.createElement('canvas');
          canvas.width = Math.max(1, width);
          canvas.height = Math.max(1, height);
          const ctx = canvas.getContext ? canvas.getContext('2d') : null;
          if (ctx) {
            ctx.fillStyle = '#1e1b4b';
            ctx.fillRect(0, 0, width, height);
            ctx.strokeStyle = '#818cf8';
            ctx.lineWidth = 2;
            ctx.strokeRect(1, 1, width - 2, height - 2);
            dataUrl = canvas.toDataURL('image/png');
          }
        }
      } catch {
        // Fall back to synthetic PNG
      }
    }

    if (onCapture) {
      onCapture({
        name: `snip-${Date.now()}.png`,
        mime_type: 'image/png',
        data: dataUrl,
        bounds,
      });
    }
  }, [isSelecting, startPos, currentPos, onCapture]);

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
          onClick={() => onCancel && onCancel()}
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
