import React from 'react';
import {
  AlertTriangle,
  MessageSquare,
  Plus,
  X
} from 'lucide-react';
import { toFileUrl } from './renderers/fileUrl';
import { formatBytes } from '../formatBytes';

// Wave 5 (Annex C row 10): the detail-modal seam, split out of
// MediaLibrary.jsx. The library owns the edit state (tags/notes)
// and the save/jump handlers; this component is the modal's
// presentation — preview, session attribution, tag/notes editors
// and the queued-state notices — driven by props.

export default function MediaDetailModal({
  item,
  sessionTitle,
  editTags = [],
  tagInput = '',
  editNotes = '',
  thumbFailed = false,
  deleteQueued = false,
  metaQueued = false,
  getOriginLabel,
  getOriginBadgeStyle,
  onClose,
  onSave,
  onJumpToChat,
  onAddTag,
  onRemoveTag,
  onTagInputChange,
  onNotesChange,
  onThumbError,
}) {
  return (

      <div
        data-testid="media-detail-modal"
        className="aether-scrim"
        style={{
          position: 'fixed',
          inset: 0,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          zIndex: 1000,
        }}
        onClick={onClose}
      >
        <div
          className="aether-glass"
          style={{
            width: '560px',
            maxWidth: '92vw',
            maxHeight: '88vh',
            overflowY: 'auto',
            padding: '24px',
            display: 'flex',
            flexDirection: 'column',
            gap: '16px',
          }}
          onClick={(e) => e.stopPropagation()}
        >
          {/* Modal Header */}
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <span
                style={{
                  fontSize: '11px',
                  fontWeight: 600,
                  padding: '2px 8px',
                  borderRadius: '999px',
                  ...getOriginBadgeStyle(item.origin, item.collection)
                }}
              >
                {getOriginLabel(item.origin, item.collection)}
              </span>
              <span style={{ fontSize: '11px', fontFamily: 'var(--font-mono)', color: 'var(--text-muted)' }}>
                {formatBytes(item.file_size)}
              </span>
            </div>
            <button
              type="button"
              data-testid="media-modal-close-btn"
              onClick={onClose}
              style={{
                background: 'transparent',
                border: 'none',
                color: 'var(--text-muted)',
                cursor: 'pointer',
                padding: '4px'
              }}
            >
              <X size={18} />
            </button>
          </div>

          {/* Title / Path */}
          <div>
            <h3 style={{ margin: '0 0 4px 0', fontSize: '16px', color: 'var(--text-primary)' }}>
              {item.file_path.split(/[/\\]/).pop()}
            </h3>
            <div style={{ fontSize: '11px', fontFamily: 'var(--font-mono)', color: 'var(--text-muted)', wordBreak: 'break-all' }}>
              {item.file_path}
            </div>
          </div>

          {/* D3-07: the detail view shows the media itself, not
              only its metadata — same file:// seam as the grid. */}
          {item.file_exists !== false
            && toFileUrl(item.file_path)
            && !thumbFailed && (
            item.media_type === 'video' ? (
              <video
                data-testid="media-detail-preview"
                src={toFileUrl(item.file_path)}
                controls
                preload="metadata"
                onError={() => onThumbError(item.id)}
                style={{ width: '100%', maxHeight: '320px', borderRadius: '10px', background: 'var(--bg-base)' }}
              />
            ) : (
              <img
                data-testid="media-detail-preview"
                src={toFileUrl(item.file_path)}
                alt={item.file_path.split(/[/\\]/).pop()}
                onError={() => onThumbError(item.id)}
                style={{ width: '100%', maxHeight: '320px', objectFit: 'contain', borderRadius: '10px', background: 'var(--bg-base)' }}
              />
            )
          )}

          {/* Missing file alert inside modal */}
          {item.file_exists === false && (
            <div
              style={{
                background: 'rgba(201, 42, 42, 0.12)',
                border: '1px solid rgba(201, 42, 42, 0.3)',
                borderRadius: 'var(--radius-md)',
                padding: '10px 14px',
                display: 'flex',
                alignItems: 'center',
                gap: '10px',
                color: 'var(--danger)',
                fontSize: '12px'
              }}
            >
              <AlertTriangle size={18} />
              <span>The file at this path is missing or was moved externally.</span>
            </div>
          )}

          {/* Session attribution */}
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', background: 'var(--bg-raised)', padding: '10px 14px', borderRadius: 'var(--radius-md)' }}>
            <div>
              <div style={{ fontSize: '10px', color: 'var(--text-muted)', textTransform: 'uppercase' }}>Originating Session</div>
              <div style={{ fontSize: '12px', color: 'var(--text-primary)', fontWeight: 500 }}>
                {sessionTitle}
              </div>
            </div>
            {item.session_id && (
              <button
                type="button"
                data-testid={`jump-to-conversation-${item.id}`}
                onClick={(e) => onJumpToChat(item, e)}
                style={{
                  background: 'rgba(var(--accent-rgb), 0.15)',
                  border: '1px solid rgba(var(--accent-rgb), 0.35)',
                  color: 'var(--accent)',
                  borderRadius: 'var(--radius-sm)',
                  padding: '6px 12px',
                  fontSize: '11px',
                  fontWeight: 500,
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px'
                }}
              >
                <MessageSquare size={13} />
                <span>Jump to Conversation</span>
              </button>
            )}
          </div>

          {/* Tags Section */}
          <div>
            <label style={{ display: 'block', fontSize: '12px', fontWeight: 600, color: 'var(--text-secondary)', marginBottom: '8px' }}>
              Tags
            </label>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', marginBottom: '10px' }}>
              {editTags.map((tag, idx) => (
                <span
                  key={idx}
                  data-testid="media-tag-item"
                  style={{
                    background: 'var(--bg-overlay)',
                    border: '1px solid var(--border-strong)',
                    color: 'var(--text-primary)',
                    fontSize: '11px',
                    padding: '3px 8px',
                    borderRadius: 'var(--radius-sm)',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '5px'
                  }}
                >
                  <span>#{tag}</span>
                  <button
                    type="button"
                    onClick={() => onRemoveTag(tag)}
                    style={{
                      background: 'transparent',
                      border: 'none',
                      color: 'var(--text-muted)',
                      cursor: 'pointer',
                      padding: '0',
                      display: 'flex'
                    }}
                  >
                    <X size={11} />
                  </button>
                </span>
              ))}
            </div>
            <div style={{ display: 'flex', gap: '8px' }}>
              <input
                type="text"
                className="aether-search-input"
                data-testid="media-tag-input"
                placeholder="Add a tag..."
                value={tagInput}
                onChange={(e) => onTagInputChange(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    onAddTag();
                  }
                }}
                style={{
                  flex: 1,
                  background: 'var(--bg-base)',
                  border: '1px solid var(--border-default)',
                  borderRadius: 'var(--radius-md)',
                  padding: '8px 12px',
                  color: 'var(--text-primary)',
                  fontSize: '12px'
                }}
              />
              <button
                type="button"
                data-testid="media-add-tag-btn"
                onClick={onAddTag}
                style={{
                  background: 'var(--bg-hover)',
                  border: '1px solid var(--border-default)',
                  color: 'var(--text-primary)',
                  borderRadius: 'var(--radius-md)',
                  padding: '0 12px',
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '4px',
                  fontSize: '12px'
                }}
              >
                <Plus size={14} />
                <span>Add</span>
              </button>
            </div>
          </div>

          {/* Notes Section */}
          <div>
            <label style={{ display: 'block', fontSize: '12px', fontWeight: 600, color: 'var(--text-secondary)', marginBottom: '8px' }}>
              Notes
            </label>
            <textarea
              className="aether-search-input"
              data-testid="media-notes-textarea"
              rows={3}
              placeholder="Add user notes, observations, or context..."
              value={editNotes}
              onChange={(e) => onNotesChange(e.target.value)}
              style={{
                width: '100%',
                background: 'var(--bg-base)',
                border: '1px solid var(--border-default)',
                borderRadius: 'var(--radius-md)',
                padding: '10px 12px',
                color: 'var(--text-primary)',
                fontSize: '12px',
                resize: 'vertical',
                boxSizing: 'border-box'
              }}
            />
          </div>

          {/* RUX-A-09: queued state for THIS item, derived from
              the real queue — an earlier offline save/delete is
              still waiting to land, so say so here too instead of
              letting a second edit stack onto the pending one. */}
          {deleteQueued && (
            <div
              data-testid="media-detail-delete-queued"
              style={{ fontSize: '12px', color: 'var(--accent)', background: 'rgba(var(--accent-rgb), 0.08)', border: '1px solid rgba(var(--accent-rgb), 0.3)', borderRadius: 'var(--radius-md)', padding: '8px 12px' }}
            >
              Delete queued — this item will be removed when the engine reconnects.
            </div>
          )}
          {metaQueued && (
            <div
              data-testid="media-detail-meta-queued"
              style={{ fontSize: '12px', color: 'var(--accent)', background: 'rgba(var(--accent-rgb), 0.08)', border: '1px solid rgba(var(--accent-rgb), 0.3)', borderRadius: 'var(--radius-md)', padding: '8px 12px' }}
            >
              An edit for this item is queued — it will apply when the engine reconnects.
            </div>
          )}

          {/* Actions */}
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px', marginTop: '10px' }}>
            <button
              type="button"
              onClick={onClose}
              style={{
                background: 'transparent',
                border: '1px solid var(--border-strong)',
                color: 'var(--text-muted)',
                borderRadius: 'var(--radius-md)',
                padding: '8px 16px',
                fontSize: '12px',
                cursor: 'pointer'
              }}
            >
              Cancel
            </button>
            <button
              type="button"
              data-testid="media-save-metadata-btn"
              onClick={onSave}
              disabled={metaQueued}
              style={{
                background: 'var(--accent)',
                border: 'none',
                color: 'var(--accent-on)',
                borderRadius: 'var(--radius-md)',
                padding: '8px 18px',
                fontSize: '12px',
                fontWeight: 600,
                cursor: metaQueued ? 'default' : 'pointer',
                opacity: metaQueued ? 0.55 : 1
              }}
            >
              Save Changes
            </button>
          </div>
        </div>
      </div>
  );
}
