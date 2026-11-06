import React from 'react';
import {
  Image as ImageIcon,
  Video as VideoIcon,
  AlertTriangle,
  Trash2,
  MessageSquare,
  FolderGit2
} from 'lucide-react';
import { toFileUrl } from './renderers/fileUrl';
import { formatBytes } from '../formatBytes';

// Wave 5 (Annex C row 10): the media grid seam, split out of
// MediaLibrary.jsx. The library keeps the list/search/delete state
// machines and the load/empty states; this component is the
// populated grid itself — one card per item, with the queued
// markings and session attribution derived from the props the
// library passes down.

// Format timestamp
function formatTimestamp(ts) {
  if (!ts) return '—';
  try {
    const date = new Date(ts);
    return date.toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit'
    });
  } catch {
    return '—';
  }
}

export default function MediaGrid({
  items = [],
  getSessionTitle,
  failedThumbs,
  onThumbError,
  onOpenDetail,
  onDeletePrompt,
  onJumpToChat,
  pendingDeleteFor,
  pendingMetaFor,
  getOriginLabel,
  getOriginBadgeStyle,
}) {
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))',
        gap: '16px',
        paddingBottom: '20px'
      }}
    >
      {items.map(item => {
        const filename = (item.file_path || '').split(/[/\\]/).pop() || 'Media File';
        const sessTitle = getSessionTitle(item.session_id);
        const originLabel = getOriginLabel(item.origin, item.collection);
        const isMissing = item.file_exists === false;
        // RUX-A-09: queued markings derive from the real
        // offline queue — they clear themselves on flush.
        const deleteQueued = Boolean(pendingDeleteFor(item.id));
        const metaQueued = Boolean(pendingMetaFor(item.id));
        // D3-07: the card renders the media itself through
        // the app's existing file:// seam (the same one
        // ProjectPreview uses for local artifacts) — until
        // now it rendered only an icon and metadata text,
        // so the library never showed the media at all. A
        // failed load falls back to the placeholder below.
        const thumbUrl = !isMissing && !failedThumbs.has(item.id)
          ? toFileUrl(item.file_path)
          : '';

        return (
          <div
            key={item.id}
            data-testid={`media-card-${item.id}`}
            onClick={() => onOpenDetail(item)}
            style={{
              background: 'rgba(20, 22, 30, 0.7)',
              border: isMissing ? '1px solid rgba(239, 68, 68, 0.4)' : '1px solid rgba(255, 255, 255, 0.08)',
              borderRadius: '12px',
              overflow: 'hidden',
              display: 'flex',
              flexDirection: 'column',
              cursor: 'pointer',
              transition: 'all 0.2s',
              position: 'relative'
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.borderColor = isMissing
                ? 'rgba(239, 68, 68, 0.7)'
                : 'rgba(139, 92, 246, 0.5)';
              e.currentTarget.style.transform = 'translateY(-2px)';
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.borderColor = isMissing
                ? 'rgba(239, 68, 68, 0.4)'
                : 'rgba(255, 255, 255, 0.08)';
              e.currentTarget.style.transform = 'none';
            }}
          >
            {/* Thumbnail / Preview Area */}
            <div
              style={{
                height: '140px',
                background: 'rgba(0, 0, 0, 0.4)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                position: 'relative',
                borderBottom: '1px solid rgba(255, 255, 255, 0.05)'
              }}
            >
              {isMissing ? (
                <div
                  data-testid={`media-missing-badge-${item.id}`}
                  style={{
                    display: 'flex',
                    flexDirection: 'column',
                    alignItems: 'center',
                    gap: '6px',
                    color: '#f87171'
                  }}
                >
                  <AlertTriangle size={28} />
                  <span style={{ fontSize: '11px', fontWeight: 600 }}>Missing File</span>
                </div>
              ) : thumbUrl && item.media_type === 'video' ? (
                <video
                  data-testid={`media-thumb-${item.id}`}
                  src={thumbUrl}
                  preload="metadata"
                  muted
                  playsInline
                  onError={() => onThumbError(item.id)}
                  style={{ width: '100%', height: '100%', objectFit: 'cover' }}
                />
              ) : thumbUrl ? (
                <img
                  data-testid={`media-thumb-${item.id}`}
                  src={thumbUrl}
                  alt={filename}
                  onError={() => onThumbError(item.id)}
                  style={{ width: '100%', height: '100%', objectFit: 'cover' }}
                />
              ) : item.media_type === 'video' ? (
                <div
                  style={{
                    display: 'flex',
                    flexDirection: 'column',
                    alignItems: 'center',
                    gap: '8px',
                    color: '#94a3b8'
                  }}
                >
                  <div
                    style={{
                      width: '44px',
                      height: '44px',
                      borderRadius: '50%',
                      background: 'rgba(255, 255, 255, 0.08)',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center'
                    }}
                  >
                    <VideoIcon size={22} color="#a78bfa" />
                  </div>
                  <span style={{ fontSize: '11px', color: '#94a3b8' }}>Video Clip</span>
                </div>
              ) : (
                <div
                  style={{
                    display: 'flex',
                    flexDirection: 'column',
                    alignItems: 'center',
                    gap: '8px',
                    color: '#94a3b8'
                  }}
                >
                  <div
                    style={{
                      width: '44px',
                      height: '44px',
                      borderRadius: '50%',
                      background: 'rgba(255, 255, 255, 0.08)',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center'
                    }}
                  >
                    <ImageIcon size={22} color="#f472b6" />
                  </div>
                  <span style={{ fontSize: '11px', color: '#94a3b8' }}>Image</span>
                </div>
              )}

              {/* Media Type pill on top-left of thumbnail */}
              <span
                style={{
                  position: 'absolute',
                  top: '8px',
                  left: '8px',
                  fontSize: '9px',
                  fontWeight: 700,
                  padding: '2px 6px',
                  borderRadius: '4px',
                  background: 'rgba(0, 0, 0, 0.65)',
                  color: '#cbd5e1',
                  letterSpacing: '0.5px'
                }}
              >
                {item.media_type ? item.media_type.toUpperCase() : 'MEDIA'}
              </span>

              {/* Delete action button on top-right */}
              <button
                type="button"
                data-testid={`media-delete-btn-${item.id}`}
                onClick={(e) => onDeletePrompt(item, e)}
                disabled={deleteQueued}
                title={deleteQueued ? 'Delete queued — will apply when the engine reconnects' : 'Delete from library'}
                style={{
                  position: 'absolute',
                  top: '8px',
                  right: '8px',
                  background: 'rgba(0, 0, 0, 0.65)',
                  border: 'none',
                  borderRadius: '6px',
                  color: '#94a3b8',
                  padding: '5px',
                  cursor: deleteQueued ? 'default' : 'pointer',
                  opacity: deleteQueued ? 0.45 : 1,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  transition: 'all 0.15s'
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.color = '#ef4444';
                  e.currentTarget.style.background = 'rgba(239, 68, 68, 0.2)';
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.color = '#94a3b8';
                  e.currentTarget.style.background = 'rgba(0, 0, 0, 0.65)';
                }}
              >
                <Trash2 size={13} />
              </button>
            </div>

            {/* Card Body */}
            <div style={{ padding: '12px', display: 'flex', flexDirection: 'column', gap: '8px', flex: 1 }}>
              {/* Filename & Origin Badge */}
              <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '8px' }}>
                <div
                  style={{
                    fontWeight: 600,
                    fontSize: '13px',
                    color: '#f8fafc',
                    whiteSpace: 'nowrap',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    flex: 1
                  }}
                  title={filename}
                >
                  {filename}
                </div>
                <span
                  data-testid={`media-origin-${item.id}`}
                  style={{
                    fontSize: '10px',
                    fontWeight: 600,
                    padding: '2px 7px',
                    borderRadius: '999px',
                    whiteSpace: 'nowrap',
                    ...getOriginBadgeStyle(item.origin, item.collection)
                  }}
                >
                  {originLabel}
                </span>
              </div>

              {/* Path */}
              <div
                style={{
                  fontSize: '11px',
                  color: '#64748b',
                  whiteSpace: 'nowrap',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis'
                }}
                title={item.file_path}
              >
                {item.file_path}
              </div>

              {/* Session Attribution */}
              {item.session_id && (
                <div
                  data-testid={`media-session-${item.id}`}
                  style={{
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: '5px',
                    color: '#a78bfa',
                    fontSize: '11px',
                    background: 'rgba(139, 92, 246, 0.1)',
                    padding: '2px 8px',
                    borderRadius: '6px',
                    width: 'fit-content',
                    maxWidth: '100%',
                    whiteSpace: 'nowrap',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis'
                  }}
                >
                  <FolderGit2 size={12} />
                  <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{sessTitle}</span>
                </div>
              )}

              {/* Tags Pills */}
              {Array.isArray(item.tags) && item.tags.length > 0 && (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px', marginTop: '2px' }}>
                  {item.tags.slice(0, 3).map((tag, tIdx) => (
                    <span
                      key={tIdx}
                      style={{
                        fontSize: '10px',
                        padding: '1px 6px',
                        borderRadius: '4px',
                        background: 'rgba(255, 255, 255, 0.08)',
                        color: '#cbd5e1'
                      }}
                    >
                      #{tag}
                    </span>
                  ))}
                  {item.tags.length > 3 && (
                    <span style={{ fontSize: '10px', color: '#64748b' }}>+{item.tags.length - 3}</span>
                  )}
                </div>
              )}

              {/* RUX-A-09: queued markings — the action is in
                  the offline queue, NOT applied. Shown until
                  the flush + reply lands the real outcome. */}
              {(deleteQueued || metaQueued) && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                  {deleteQueued && (
                    <span
                      data-testid={`media-delete-queued-${item.id}`}
                      style={{ fontSize: '11px', color: '#fbbf24', fontWeight: 500 }}
                    >
                      Delete queued — will apply when the engine reconnects.
                    </span>
                  )}
                  {metaQueued && (
                    <span
                      data-testid={`media-meta-queued-${item.id}`}
                      style={{ fontSize: '11px', color: '#fbbf24', fontWeight: 500 }}
                    >
                      Edit queued — will apply when the engine reconnects.
                    </span>
                  )}
                </div>
              )}

              {/* Footer: Size/Date + Jump to Conversation */}
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  marginTop: 'auto',
                  paddingTop: '8px',
                  borderTop: '1px solid rgba(255, 255, 255, 0.05)',
                  fontSize: '11px',
                  color: '#64748b'
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                  <span>{formatBytes(item.file_size)}</span>
                  <span>•</span>
                  <span>{formatTimestamp(item.created_at)}</span>
                </div>

                {item.session_id && (
                  <button
                    type="button"
                    data-testid={`jump-to-conversation-${item.id}`}
                    onClick={(e) => onJumpToChat(item, e)}
                    title="Open conversation and scroll to message"
                    style={{
                      background: 'transparent',
                      border: 'none',
                      color: '#a78bfa',
                      cursor: 'pointer',
                      display: 'flex',
                      alignItems: 'center',
                      gap: '4px',
                      padding: '3px 6px',
                      borderRadius: '4px',
                      fontWeight: 500,
                      fontSize: '11px',
                      transition: 'all 0.15s'
                    }}
                    onMouseEnter={(e) => {
                      e.currentTarget.style.background = 'rgba(139, 92, 246, 0.15)';
                    }}
                    onMouseLeave={(e) => {
                      e.currentTarget.style.background = 'transparent';
                    }}
                  >
                    <MessageSquare size={12} />
                    <span>Jump to Chat</span>
                  </button>
                )}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
