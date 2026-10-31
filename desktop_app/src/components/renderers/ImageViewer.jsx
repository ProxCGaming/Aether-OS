import React, { useState } from 'react';
import { ZoomIn, ZoomOut, RotateCcw } from 'lucide-react';

export default function ImageViewer({ src = '', alt = 'Artifact Image' }) {
  const [zoom, setZoom] = useState(100);

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

      {/* Image canvas */}
      <div
        style={{
          flex: 1,
          overflow: 'auto',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          padding: '24px'
        }}
      >
        <img
          src={src}
          alt={alt}
          style={{
            maxWidth: '100%',
            maxHeight: '100%',
            transform: `scale(${zoom / 100})`,
            transformOrigin: 'center center',
            transition: 'transform 150ms ease-out',
            boxShadow: '0 8px 32px rgba(0, 0, 0, 0.5)',
            borderRadius: '4px'
          }}
        />
      </div>
    </div>
  );
}
