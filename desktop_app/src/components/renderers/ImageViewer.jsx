import React, { useState } from 'react';
import { ZoomIn, ZoomOut, RotateCcw, AlertCircle } from 'lucide-react';

export default function ImageViewer({ src = '', alt = 'Artifact Image' }) {
  const [zoom, setZoom] = useState(100);
  // D3-12: a failed load is a visible state, not an empty box with
  // working-looking zoom controls. The failure is recorded against
  // the src that produced it, so a new src starts clean without a
  // reset effect.
  const [failedSrc, setFailedSrc] = useState(null);
  const loadError = failedSrc !== null && failedSrc === src;

  const handleZoomIn = () => setZoom((prev) => Math.min(prev + 25, 400));
  const handleZoomOut = () => setZoom((prev) => Math.max(prev - 25, 25));
  const handleReset = () => setZoom(100);

  return (
    <div
      data-testid="image-viewer"
      style={{
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        background: 'rgba(15, 15, 20, 0.75)',
        borderRadius: '8px',
        border: '1px solid rgba(255, 255, 255, 0.08)',
        overflow: 'hidden'
      }}
    >
      {/* Controls toolbar */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
          padding: '8px 12px',
          borderBottom: '1px solid rgba(255, 255, 255, 0.08)',
          background: 'rgba(255, 255, 255, 0.02)'
        }}
      >
        <button
          type="button"
          data-testid="zoom-in"
          onClick={handleZoomIn}
          title="Zoom In"
          style={{
            background: 'rgba(255, 255, 255, 0.08)',
            border: 'none',
            borderRadius: '6px',
            color: '#fff',
            padding: '4px 8px',
            cursor: 'pointer',
            display: 'flex',
            alignItems: 'center'
          }}
        >
          <ZoomIn size={14} />
        </button>
        <button
          type="button"
          data-testid="zoom-out"
          onClick={handleZoomOut}
          title="Zoom Out"
          style={{
            background: 'rgba(255, 255, 255, 0.08)',
            border: 'none',
            borderRadius: '6px',
            color: '#fff',
            padding: '4px 8px',
            cursor: 'pointer',
            display: 'flex',
            alignItems: 'center'
          }}
        >
          <ZoomOut size={14} />
        </button>
        <button
          type="button"
          data-testid="zoom-reset"
          onClick={handleReset}
          title="Reset Zoom"
          style={{
            background: 'rgba(255, 255, 255, 0.08)',
            border: 'none',
            borderRadius: '6px',
            color: '#fff',
            padding: '4px 8px',
            cursor: 'pointer',
            display: 'flex',
            alignItems: 'center'
          }}
        >
          <RotateCcw size={14} />
        </button>
        <span
          data-testid="zoom-level"
          style={{ fontSize: '12px', color: '#94a3b8', marginLeft: '4px', userSelect: 'none' }}
        >
          {zoom}%
        </span>
      </div>

      {/* Image canvas. D3-12: zoom is a real layout size (width %),
          not a transform — a transform leaves the layout box at
          fit-size inside the scroll container, so at 200-400% the
          overflowed edges have no scroll range and are simply
          unreachable. margin:auto (instead of align/justify
          centering) keeps an overflowing image scrollable to every
          edge, and flexShrink: 0 stops the flex row from capping
          the zoomed width at the container size. */}
      <div
        style={{
          flex: 1,
          overflow: 'auto',
          display: 'flex',
          padding: '24px'
        }}
      >
        {loadError ? (
          <div
            data-testid="image-load-error"
            style={{
              margin: 'auto',
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              gap: '8px',
              color: '#fca5a5',
              fontSize: '13px',
              textAlign: 'center'
            }}
          >
            <AlertCircle size={28} color="#ef4444" />
            <span>Image could not be loaded.</span>
            <span style={{ color: 'rgba(255, 255, 255, 0.4)', fontSize: '11px' }}>{alt}</span>
          </div>
        ) : (
          <img
            src={src}
            alt={alt}
            onError={() => setFailedSrc(src)}
            style={{
              margin: 'auto',
              flexShrink: 0,
              width: `${zoom}%`,
              height: 'auto',
              transition: 'width 150ms ease-out',
              boxShadow: '0 8px 32px rgba(0, 0, 0, 0.5)',
              borderRadius: '4px'
            }}
          />
        )}
      </div>
    </div>
  );
}
