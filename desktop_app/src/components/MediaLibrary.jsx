import React, { useState, useEffect, useMemo, useCallback } from 'react';
import {
  Search,
  Image as ImageIcon,
  Video as VideoIcon,
  Crop,
  Sparkles,
  Upload,
  AlertTriangle,
  MessageSquare,
  Trash2,
  X,
  Plus,
  HardDrive,
  FolderGit2
} from 'lucide-react';
import '../chat.css';

export default function MediaLibrary({
  media: controlledMedia,
  initialMedia,
  sessions = [],
  wsRef,
  onNavigateToSession = () => {}
}) {
  const [internalMedia, setInternalMedia] = useState(() => initialMedia || []);
  const [searchQuery, setSearchQuery] = useState('');
  const [activeCollection, setActiveCollection] = useState('all'); // 'all' | 'user' | 'snips' | 'generated'
  const [selectedMedia, setSelectedMedia] = useState(null); // for detail modal
  const [deleteTarget, setDeleteTarget] = useState(null); // for delete confirmation modal
  const [deleteUnderlyingFile, setDeleteUnderlyingFile] = useState(false);

  // Detail modal edit state
  const [editTags, setEditTags] = useState([]);
  const [tagInput, setTagInput] = useState('');
  const [editNotes, setEditNotes] = useState('');

  const effectiveMedia = controlledMedia ?? internalMedia;

  // Request media list on mount or WebSocket ready
  useEffect(() => {
    const ws = wsRef?.current;
    if (!ws) return;

    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'MEDIA_LIST_RESPONSE') {
          if (Array.isArray(data.payload?.media)) {
            setInternalMedia(data.payload.media);
          }
        } else if (data.type === 'MEDIA_RECORDED') {
          if (data.payload?.id) {
            setInternalMedia(prev => [data.payload, ...prev.filter(m => m.id !== data.payload.id)]);
          }
        } else if (data.type === 'MEDIA_DELETE_RESPONSE') {
          const deletedId = data.payload?.media_id;
          if (deletedId) {
            setInternalMedia(prev => prev.filter(m => m.id !== deletedId));
            if (selectedMedia?.id === deletedId) setSelectedMedia(null);
          }
        } else if (data.type === 'MEDIA_METADATA_UPDATE_RESPONSE') {
          const updatedId = data.payload?.media_id;
          if (updatedId) {
            setInternalMedia(prev => prev.map(m => {
              if (m.id === updatedId) {
                return {
                  ...m,
                  tags: data.payload.tags ?? m.tags,
                  user_notes: data.payload.notes ?? m.user_notes
                };
              }
              return m;
            }));
            if (selectedMedia?.id === updatedId) {
              setSelectedMedia(prev => prev ? {
                ...prev,
                tags: data.payload.tags ?? prev.tags,
                user_notes: data.payload.notes ?? prev.user_notes
              } : null);
            }
          }
        }
      } catch {
        // ignore parse error
      }
    };

    ws.addEventListener('message', handleMessage);

    if (!controlledMedia && !initialMedia && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: 'MEDIA_LIST_REQUEST',
        schema_version: 1,
        request_id: `media_list_${Date.now()}`,
        payload: { collection: 'all' }
      }));
    }

    return () => {
      ws.removeEventListener('message', handleMessage);
    };
  }, [wsRef, controlledMedia, initialMedia, selectedMedia]);

  // Session title resolver
  const getSessionTitle = useCallback((sessionId) => {
    const s = sessions.find(item => item.id === sessionId);
    return s?.title || sessionId || 'Unknown Session';
  }, [sessions]);

  // Format file size
  const formatFileSize = (bytes) => {
    if (!bytes || bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
  };

  // Format timestamp
  const formatTimestamp = (ts) => {
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
  };

  // Filtered media
  const filteredMedia = useMemo(() => {
    return effectiveMedia.filter(item => {
      // Collection filter
      const origin = (item.origin || item.probe_metadata?.origin || '').toLowerCase();
      const collection = (item.collection || '').toLowerCase();

      if (activeCollection === 'generated') {
        if (collection !== 'generated' && origin !== 'agent_generated') return false;
      } else if (activeCollection === 'snips') {
        if (origin !== 'snip') return false;
      } else if (activeCollection === 'user') {
        if (collection !== 'user' && origin !== 'user_upload' && origin !== 'snip') return false;
      }

      // Search query
      if (searchQuery.trim()) {
        const q = searchQuery.toLowerCase().trim();
        const path = (item.file_path || '').toLowerCase();
        const filename = path.split(/[/\\]/).pop() || '';
        const notes = (item.user_notes || '').toLowerCase();
        const tags = (item.tags || []).join(' ').toLowerCase();
        const sessTitle = getSessionTitle(item.session_id).toLowerCase();
        const summary = (item.summary_text || '').toLowerCase();
        return (
          filename.includes(q) ||
          path.includes(q) ||
          notes.includes(q) ||
          tags.includes(q) ||
          sessTitle.includes(q) ||
          summary.includes(q)
        );
      }

      return true;
    });
  }, [effectiveMedia, activeCollection, searchQuery, getSessionTitle]);

  // Jump to conversation handler
  const handleJumpToChat = (item, e) => {
    if (e) e.stopPropagation();
    if (onNavigateToSession) {
      onNavigateToSession({
        ...item,
        session_id: item.session_id,
        message_id: item.message_id,
        id: item.id
      });
    }

    // Attempt direct DOM scroll into view if message element exists
    if (item.message_id) {
      setTimeout(() => {
        const el =
          document.getElementById(`message-${item.message_id}`) ||
          document.querySelector(`[data-message-id="${item.message_id}"]`) ||
          document.querySelector(`[data-msg-id="${item.message_id}"]`);
        if (el) {
          el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
      }, 100);
    }
  };

  // Open detail modal
  const handleOpenDetail = (item) => {
    setSelectedMedia(item);
    setEditTags(Array.isArray(item.tags) ? [...item.tags] : []);
    setEditNotes(item.user_notes || '');
    setTagInput('');
  };

  // Add tag
  const handleAddTag = () => {
    const val = tagInput.trim();
    if (val && !editTags.includes(val)) {
      setEditTags([...editTags, val]);
      setTagInput('');
    }
  };

  // Remove tag
  const handleRemoveTag = (tagToRemove) => {
    setEditTags(editTags.filter(t => t !== tagToRemove));
  };

  // Save metadata
  const handleSaveMetadata = () => {
    if (!selectedMedia) return;
    const mediaId = selectedMedia.id;
    const tags = editTags;
    const notes = editNotes;

    // Local optimistic update
    setInternalMedia(prev => prev.map(m => {
      if (m.id === mediaId) {
        return { ...m, tags, user_notes: notes };
      }
      return m;
    }));

    // Send WS update
    const ws = wsRef?.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: 'MEDIA_METADATA_UPDATE_REQUEST',
        schema_version: 1,
        request_id: `meta_update_${Date.now()}`,
        payload: {
          media_id: mediaId,
          tags,
          notes
        }
      }));
    }

    setSelectedMedia(null);
  };

  // Trigger delete prompt
  const handleDeletePrompt = (item, e) => {
    if (e) e.stopPropagation();
    setDeleteTarget(item);
    setDeleteUnderlyingFile(false);
  };

  // Confirm delete
  const handleConfirmDelete = () => {
    if (!deleteTarget) return;
    const mid = deleteTarget.id;
    const delFile = deleteUnderlyingFile;

    // Optimistic removal
    setInternalMedia(prev => prev.filter(m => m.id !== mid));

    const ws = wsRef?.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: 'MEDIA_DELETE_REQUEST',
        schema_version: 1,
        request_id: `media_delete_${Date.now()}`,
        payload: {
          media_id: mid,
          delete_underlying_file: delFile
        }
      }));
    }

    setDeleteTarget(null);
    if (selectedMedia?.id === mid) setSelectedMedia(null);
  };

  const getOriginLabel = (origin, collection) => {
    const norm = (origin || '').toLowerCase();
    if (norm === 'snip') return 'Snip';
    if (norm === 'user_upload') return 'User upload';
    if (norm === 'agent_generated' || collection === 'generated') return 'Agent generated';
    return collection === 'generated' ? 'Agent generated' : 'User upload';
  };

  const getOriginBadgeStyle = (origin, collection) => {
    const norm = (origin || '').toLowerCase();
    if (norm === 'snip') {
      return {
        background: 'rgba(236, 72, 153, 0.18)',
        color: '#f472b6',
        border: '1px solid rgba(236, 72, 153, 0.35)'
      };
    }
    if (norm === 'agent_generated' || collection === 'generated') {
      return {
        background: 'rgba(52, 211, 153, 0.18)',
        color: '#34d399',
        border: '1px solid rgba(52, 211, 153, 0.35)'
      };
    }
    return {
      background: 'rgba(56, 189, 248, 0.18)',
      color: '#38bdf8',
      border: '1px solid rgba(56, 189, 248, 0.35)'
    };
  };

  return (
    <div
      data-testid="media-library-view"
      style={{
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        color: '#f0f0f5',
        position: 'relative'
      }}
    >
      {/* Top Toolbar: Search + Collection Filter Chips */}
      <div
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          gap: '12px',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: '16px'
        }}
      >
        {/* Search */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            background: 'rgba(0, 0, 0, 0.25)',
            border: '1px solid rgba(255, 255, 255, 0.1)',
            borderRadius: '8px',
            padding: '6px 12px',
            gap: '8px',
            width: '320px',
            maxWidth: '100%'
          }}
        >
          <Search size={15} color="#94a3b8" />
          <input
            type="text"
            data-testid="media-search-input"
            placeholder="Search by filename, tags, notes..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            style={{
              background: 'transparent',
              border: 'none',
              outline: 'none',
              color: '#fff',
              fontSize: '12px',
              width: '100%'
            }}
          />
          {searchQuery && (
            <button
              type="button"
              onClick={() => setSearchQuery('')}
              style={{
                background: 'transparent',
                border: 'none',
                color: '#94a3b8',
                cursor: 'pointer',
                padding: '2px',
                display: 'flex',
                alignItems: 'center'
              }}
            >
              <X size={13} />
            </button>
          )}
        </div>

        {/* Collection Filter Tabs */}
        <div style={{ display: 'flex', gap: '6px', alignItems: 'center' }}>
          <span
            data-testid="media-count-badge"
            style={{
              background: 'rgba(139, 92, 246, 0.25)',
              border: '1px solid rgba(139, 92, 246, 0.4)',
              color: '#c4b5fd',
              borderRadius: '999px',
              padding: '2px 8px',
              fontSize: '11px',
              fontWeight: 600,
              marginRight: '4px'
            }}
          >
            {effectiveMedia.length}
          </span>
          {[
            { id: 'all', label: 'All', icon: <HardDrive size={13} /> },
            { id: 'user', label: 'User', icon: <Upload size={13} /> },
            { id: 'snips', label: 'Snips', icon: <Crop size={13} /> },
            { id: 'generated', label: 'Generated', icon: <Sparkles size={13} /> },
          ].map(tab => {
            const isActive = activeCollection === tab.id;
            return (
              <button
                key={tab.id}
                type="button"
                data-testid={`collection-tab-${tab.id}`}
                onClick={() => setActiveCollection(tab.id)}
                style={{
                  background: isActive ? 'rgba(139, 92, 246, 0.22)' : 'rgba(255, 255, 255, 0.05)',
                  border: isActive ? '1px solid rgba(139, 92, 246, 0.45)' : '1px solid rgba(255, 255, 255, 0.08)',
                  color: isActive ? '#fff' : '#94a3b8',
                  borderRadius: '20px',
                  padding: '5px 12px',
                  cursor: 'pointer',
                  fontSize: '11px',
                  fontWeight: isActive ? 600 : 400,
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px',
                  transition: 'all 0.15s'
                }}
              >
                {tab.icon}
                <span>{tab.label}</span>
              </button>
            );
          })}
        </div>
      </div>

      {/* Main Grid View */}
      <div style={{ flex: 1, overflowY: 'auto' }} className="chat-scroll">
        {effectiveMedia.length === 0 ? (
          <div
            data-testid="empty-media-state"
            style={{
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'center',
              height: '320px',
              color: '#94a3b8',
              gap: '12px'
            }}
          >
            <div
              style={{
                width: '56px',
                height: '56px',
                borderRadius: '50%',
                background: 'rgba(255, 255, 255, 0.04)',
                border: '1px solid rgba(255, 255, 255, 0.08)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center'
              }}
            >
              <ImageIcon size={26} color="#64748b" />
            </div>
            <div style={{ fontSize: '15px', fontWeight: 500, color: '#e2e8f0' }}>No media items yet</div>
            <div style={{ fontSize: '12px', color: '#64748b', maxWidth: '360px', textAlign: 'center' }}>
              Screen snips, user-uploaded images/videos, and agent-generated visuals will appear here in-place.
            </div>
          </div>
        ) : filteredMedia.length === 0 ? (
          <div
            data-testid="empty-media-search"
            style={{
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'center',
              height: '240px',
              color: '#94a3b8',
              gap: '10px'
            }}
          >
            <Search size={28} color="#64748b" />
            <div style={{ fontSize: '14px', fontWeight: 500, color: '#e2e8f0' }}>No matching media found</div>
            <div style={{ fontSize: '12px', color: '#64748b' }}>
              No media matches &quot;{searchQuery}&quot; in the {activeCollection} collection.
            </div>
          </div>
        ) : (
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))',
              gap: '16px',
              paddingBottom: '20px'
            }}
          >
            {filteredMedia.map(item => {
              const filename = (item.file_path || '').split(/[/\\]/).pop() || 'Media File';
              const sessTitle = getSessionTitle(item.session_id);
              const originLabel = getOriginLabel(item.origin, item.collection);
              const isMissing = item.file_exists === false;

              return (
                <div
                  key={item.id}
                  data-testid={`media-card-${item.id}`}
                  onClick={() => handleOpenDetail(item)}
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
                      onClick={(e) => handleDeletePrompt(item, e)}
                      title="Delete from library"
                      style={{
                        position: 'absolute',
                        top: '8px',
                        right: '8px',
                        background: 'rgba(0, 0, 0, 0.65)',
                        border: 'none',
                        borderRadius: '6px',
                        color: '#94a3b8',
                        padding: '5px',
                        cursor: 'pointer',
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
                        <span>{formatFileSize(item.file_size)}</span>
                        <span>•</span>
                        <span>{formatTimestamp(item.created_at)}</span>
                      </div>

                      {item.session_id && (
                        <button
                          type="button"
                          data-testid={`jump-to-conversation-${item.id}`}
                          onClick={(e) => handleJumpToChat(item, e)}
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
        )}
      </div>

      {/* Detail & Metadata Edit Modal */}
      {selectedMedia && (
        <div
          data-testid="media-detail-modal"
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(0, 0, 0, 0.75)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 1000,
            backdropFilter: 'blur(4px)'
          }}
          onClick={() => setSelectedMedia(null)}
        >
          <div
            style={{
              background: '#14161f',
              border: '1px solid rgba(255, 255, 255, 0.15)',
              borderRadius: '16px',
              width: '560px',
              maxWidth: '92vw',
              maxHeight: '88vh',
              overflowY: 'auto',
              padding: '24px',
              display: 'flex',
              flexDirection: 'column',
              gap: '16px',
              boxShadow: '0 20px 40px rgba(0, 0, 0, 0.6)'
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
                    ...getOriginBadgeStyle(selectedMedia.origin, selectedMedia.collection)
                  }}
                >
                  {getOriginLabel(selectedMedia.origin, selectedMedia.collection)}
                </span>
                <span style={{ fontSize: '11px', color: '#64748b' }}>
                  {formatFileSize(selectedMedia.file_size)}
                </span>
              </div>
              <button
                type="button"
                data-testid="media-modal-close-btn"
                onClick={() => setSelectedMedia(null)}
                style={{
                  background: 'transparent',
                  border: 'none',
                  color: '#94a3b8',
                  cursor: 'pointer',
                  padding: '4px'
                }}
              >
                <X size={18} />
              </button>
            </div>

            {/* Title / Path */}
            <div>
              <h3 style={{ margin: '0 0 4px 0', fontSize: '16px', color: '#fff' }}>
                {selectedMedia.file_path.split(/[/\\]/).pop()}
              </h3>
              <div style={{ fontSize: '11px', color: '#64748b', wordBreak: 'break-all' }}>
                {selectedMedia.file_path}
              </div>
            </div>

            {/* Missing file alert inside modal */}
            {selectedMedia.file_exists === false && (
              <div
                style={{
                  background: 'rgba(239, 68, 68, 0.12)',
                  border: '1px solid rgba(239, 68, 68, 0.3)',
                  borderRadius: '8px',
                  padding: '10px 14px',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '10px',
                  color: '#f87171',
                  fontSize: '12px'
                }}
              >
                <AlertTriangle size={18} />
                <span>The file at this path is missing or was moved externally.</span>
              </div>
            )}

            {/* Session attribution */}
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', background: 'rgba(255, 255, 255, 0.03)', padding: '10px 14px', borderRadius: '8px' }}>
              <div>
                <div style={{ fontSize: '10px', color: '#64748b', textTransform: 'uppercase' }}>Originating Session</div>
                <div style={{ fontSize: '12px', color: '#e2e8f0', fontWeight: 500 }}>
                  {getSessionTitle(selectedMedia.session_id)}
                </div>
              </div>
              {selectedMedia.session_id && (
                <button
                  type="button"
                  data-testid={`jump-to-conversation-${selectedMedia.id}`}
                  onClick={(e) => handleJumpToChat(selectedMedia, e)}
                  style={{
                    background: 'rgba(139, 92, 246, 0.15)',
                    border: '1px solid rgba(139, 92, 246, 0.3)',
                    color: '#c4b5fd',
                    borderRadius: '6px',
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
              <label style={{ display: 'block', fontSize: '12px', fontWeight: 600, color: '#cbd5e1', marginBottom: '8px' }}>
                Tags
              </label>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', marginBottom: '10px' }}>
                {editTags.map((tag, idx) => (
                  <span
                    key={idx}
                    data-testid="media-tag-item"
                    style={{
                      background: 'rgba(139, 92, 246, 0.18)',
                      border: '1px solid rgba(139, 92, 246, 0.35)',
                      color: '#c4b5fd',
                      fontSize: '11px',
                      padding: '3px 8px',
                      borderRadius: '6px',
                      display: 'flex',
                      alignItems: 'center',
                      gap: '5px'
                    }}
                  >
                    <span>#{tag}</span>
                    <button
                      type="button"
                      onClick={() => handleRemoveTag(tag)}
                      style={{
                        background: 'transparent',
                        border: 'none',
                        color: '#c4b5fd',
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
                  data-testid="media-tag-input"
                  placeholder="Add a tag..."
                  value={tagInput}
                  onChange={(e) => setTagInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault();
                      handleAddTag();
                    }
                  }}
                  style={{
                    flex: 1,
                    background: 'rgba(0, 0, 0, 0.3)',
                    border: '1px solid rgba(255, 255, 255, 0.12)',
                    borderRadius: '8px',
                    padding: '8px 12px',
                    color: '#fff',
                    fontSize: '12px',
                    outline: 'none'
                  }}
                />
                <button
                  type="button"
                  data-testid="media-add-tag-btn"
                  onClick={handleAddTag}
                  style={{
                    background: 'rgba(255, 255, 255, 0.08)',
                    border: '1px solid rgba(255, 255, 255, 0.12)',
                    color: '#fff',
                    borderRadius: '8px',
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
              <label style={{ display: 'block', fontSize: '12px', fontWeight: 600, color: '#cbd5e1', marginBottom: '8px' }}>
                Notes
              </label>
              <textarea
                data-testid="media-notes-textarea"
                rows={3}
                placeholder="Add user notes, observations, or context..."
                value={editNotes}
                onChange={(e) => setEditNotes(e.target.value)}
                style={{
                  width: '100%',
                  background: 'rgba(0, 0, 0, 0.3)',
                  border: '1px solid rgba(255, 255, 255, 0.12)',
                  borderRadius: '8px',
                  padding: '10px 12px',
                  color: '#fff',
                  fontSize: '12px',
                  outline: 'none',
                  resize: 'vertical',
                  boxSizing: 'border-box'
                }}
              />
            </div>

            {/* Actions */}
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px', marginTop: '10px' }}>
              <button
                type="button"
                onClick={() => setSelectedMedia(null)}
                style={{
                  background: 'transparent',
                  border: '1px solid rgba(255, 255, 255, 0.15)',
                  color: '#94a3b8',
                  borderRadius: '8px',
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
                onClick={handleSaveMetadata}
                style={{
                  background: '#8b5cf6',
                  border: 'none',
                  color: '#fff',
                  borderRadius: '8px',
                  padding: '8px 18px',
                  fontSize: '12px',
                  fontWeight: 600,
                  cursor: 'pointer'
                }}
              >
                Save Changes
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Delete Confirmation Modal */}
      {deleteTarget && (
        <div
          data-testid="delete-confirm-modal"
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(0, 0, 0, 0.75)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 1100,
            backdropFilter: 'blur(4px)'
          }}
          onClick={() => setDeleteTarget(null)}
        >
          <div
            style={{
              background: '#14161f',
              border: '1px solid rgba(255, 255, 255, 0.15)',
              borderRadius: '16px',
              width: '420px',
              maxWidth: '90vw',
              padding: '24px',
              display: 'flex',
              flexDirection: 'column',
              gap: '16px',
              boxShadow: '0 20px 40px rgba(0, 0, 0, 0.6)'
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', color: '#ef4444' }}>
              <Trash2 size={22} />
              <h3 style={{ margin: 0, fontSize: '16px', color: '#fff' }}>Remove Media Entry</h3>
            </div>
            <p style={{ margin: 0, fontSize: '13px', color: '#94a3b8', lineHeight: 1.5 }}>
              Are you sure you want to remove this media item from your library?
            </p>

            <label
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '8px',
                fontSize: '12px',
                color: '#cbd5e1',
                cursor: 'pointer',
                background: 'rgba(255, 255, 255, 0.04)',
                padding: '8px 12px',
                borderRadius: '8px'
              }}
            >
              <input
                type="checkbox"
                data-testid="delete-file-checkbox"
                checked={deleteUnderlyingFile}
                onChange={(e) => setDeleteUnderlyingFile(e.target.checked)}
              />
              <span>Also delete underlying file from disk</span>
            </label>

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px' }}>
              <button
                type="button"
                data-testid="cancel-delete-btn"
                onClick={() => setDeleteTarget(null)}
                style={{
                  background: 'transparent',
                  border: '1px solid rgba(255, 255, 255, 0.15)',
                  color: '#94a3b8',
                  borderRadius: '8px',
                  padding: '8px 16px',
                  fontSize: '12px',
                  cursor: 'pointer'
                }}
              >
                Cancel
              </button>
              <button
                type="button"
                data-testid="confirm-delete-action-btn"
                onClick={handleConfirmDelete}
                style={{
                  background: '#ef4444',
                  border: 'none',
                  color: '#fff',
                  borderRadius: '8px',
                  padding: '8px 18px',
                  fontSize: '12px',
                  fontWeight: 600,
                  cursor: 'pointer'
                }}
              >
                Remove Entry
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
