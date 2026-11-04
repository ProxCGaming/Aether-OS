import React, { useState, useEffect, useMemo, useCallback, useRef } from 'react';
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
  safeSend = null,
  pendingMessages = [],
  reconnectCount = 0,
  onNavigateToSession = () => {}
}) {
  const [internalMedia, setInternalMedia] = useState(() => initialMedia || []);
  const [searchQuery, setSearchQuery] = useState('');
  const [activeCollection, setActiveCollection] = useState('all'); // 'all' | 'user' | 'snips' | 'generated'
  const [selectedMedia, setSelectedMedia] = useState(null); // for detail modal
  const [deleteTarget, setDeleteTarget] = useState(null); // for delete confirmation modal
  const [deleteUnderlyingFile, setDeleteUnderlyingFile] = useState(false);
  // OP12: honest delete outcome. pendingDeletesRef remembers what
  // each in-flight delete asked for (entry only, or entry + file) so
  // the response can be judged against the request; deleteNotice is
  // the surfaced result when reality fell short of the request.
  const [deleteNotice, setDeleteNotice] = useState(null); // {tone, text} | null
  const pendingDeletesRef = useRef(new Map()); // media_id -> {item, name, deleteFile}
  // N07: backend recall results for the current search query
  // ({query, items, degraded} | null). Null means "no backend answer
  // for this query" — the local metadata filter below is the fallback
  // (offline / engine unreachable), never a fabricated result set.
  const [searchResults, setSearchResults] = useState(null);

  // Detail modal edit state
  const [editTags, setEditTags] = useState([]);
  const [tagInput, setTagInput] = useState('');
  const [editNotes, setEditNotes] = useState('');

  const effectiveMedia = controlledMedia ?? internalMedia;

  // RUX-A-09: metadata save and delete are user actions — through
  // safeSend they QUEUE while the engine is unreachable instead of
  // being discarded by the readyState guard both handlers used to
  // carry *behind a local mutation applied first*, so an offline
  // edit looked saved and an offline delete looked done while the
  // engine never heard of either (edits vanished on reload, deleted
  // items resurrected on the next list load). The disposition now
  // drives the local state:
  //   'sent'   — the old optimistic behaviour stands: the reply
  //              confirms it (metadata merge is idempotent; a
  //              refused delete is restored with a notice — OP12).
  //   'queued' — NOTHING is applied locally. The item stays exactly
  //              as it is; the card/detail markings below derive
  //              from the real offline queue (pendingMessages), and
  //              the response handlers apply the real outcome when
  //              the flushed request is answered after reconnect.
  // The standalone-mount fallback keeps the exact legacy envelopes
  // (request_id prefixes included) on the unconditional facade send.
  const sendMediaAction = (type, payload, legacyRequestId) => {
    if (safeSend) return safeSend(type, payload);
    const ws = wsRef?.current;
    if (ws) {
      return ws.send(JSON.stringify({
        type,
        schema_version: 1,
        request_id: legacyRequestId,
        payload
      }));
    }
    return undefined;
  };
  const pendingEntryFor = (type, mediaId) => (pendingMessages || []).find(
    (m) => m && m.type === type && m.payload && m.payload.media_id === mediaId
  );
  const pendingDeleteFor = (mediaId) => pendingEntryFor('MEDIA_DELETE_REQUEST', mediaId);
  const pendingMetaFor = (mediaId) => pendingEntryFor('MEDIA_METADATA_UPDATE_REQUEST', mediaId);

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
            // OP12: judge the outcome against what this delete
            // actually asked for. The engine reports deleted /
            // file_deleted separately precisely so a shortfall is
            // surfaced, never smoothed over.
            const pending = pendingDeletesRef.current.get(deletedId);
            pendingDeletesRef.current.delete(deletedId);
            if (data.payload?.deleted === false) {
              // The entry was NOT removed — undo the optimistic
              // removal and say so.
              if (pending?.item && !controlledMedia) {
                setInternalMedia(prev => (
                  prev.some(m => m.id === deletedId) ? prev : [pending.item, ...prev]
                ));
              }
              setDeleteNotice({
                tone: 'error',
                text: `Could not remove ${pending?.name || 'that item'} from the library — the entry is still there.`,
              });
            } else {
              setInternalMedia(prev => prev.filter(m => m.id !== deletedId));
              if (selectedMedia?.id === deletedId) setSelectedMedia(null);
              if (pending?.deleteFile && data.payload?.file_deleted === false) {
                // Entry is gone, but the file the user explicitly
                // asked to delete is not — it may already have been
                // moved or deleted outside the app.
                setDeleteNotice({
                  tone: 'warning',
                  text: `Removed ${pending.name} from the library, but the file could not be deleted from disk — it may already have been moved or deleted.`,
                });
              }
            }
          }
        } else if (data.type === 'MEDIA_ANALYSIS_PROGRESS') {
          // N07: the production analyzer now fans its events out to
          // this connection — reflect the live analysis status.
          const progressId = data.payload?.media_id;
          const progressStatus = data.payload?.status;
          if (progressId && progressStatus) {
            setInternalMedia(prev => prev.map(m => (
              m.id === progressId ? { ...m, status: progressStatus } : m
            )));
            setSearchResults(prev => (prev ? {
              ...prev,
              items: prev.items.map(m => (
                m.id === progressId ? { ...m, status: progressStatus } : m
              ))
            } : prev));
            if (selectedMedia?.id === progressId) {
              setSelectedMedia(prev => (prev ? { ...prev, status: progressStatus } : prev));
            }
          }
        } else if (data.type === 'MEDIA_ANALYZED') {
          // N07: the full updated item (summary/tags/transcript) —
          // merge it wherever the item is currently shown.
          const analyzed = data.payload;
          if (analyzed?.id) {
            setInternalMedia(prev => prev.map(m => (m.id === analyzed.id ? { ...m, ...analyzed } : m)));
            setSearchResults(prev => (prev ? {
              ...prev,
              items: prev.items.map(m => (m.id === analyzed.id ? { ...m, ...analyzed } : m))
            } : prev));
            if (selectedMedia?.id === analyzed.id) {
              setSelectedMedia(prev => (prev ? { ...prev, ...analyzed } : prev));
            }
          }
        } else if (data.type === 'MEDIA_SEARCH_RESPONSE') {
          if (typeof data.payload?.query === 'string' && Array.isArray(data.payload?.media)) {
            setSearchResults({
              query: data.payload.query,
              items: data.payload.media,
              degraded: !!data.payload.degraded
            });
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

    // Wave 2 slice 4 (RUX-A-17): OPEN-guarded, so a mount while the
    // engine was down left the grid on "No media items yet" for the
    // whole visit; the reconnectCount dep re-runs this effect (and
    // the query) on every reconnect.
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
  }, [wsRef, controlledMedia, initialMedia, selectedMedia, reconnectCount]);

  // N07: route search through the backend recall path (transcript +
  // semantic matches) instead of only filtering the loaded page of
  // metadata locally. Debounced — every keystroke would otherwise be
  // an embedding call server-side. Stale answers need no clearing:
  // the grid only ever applies results whose query matches the
  // current input, so an empty or edited query falls back to the
  // local view on its own.
  useEffect(() => {
    const q = searchQuery.trim();
    if (!q) return undefined;
    const ws = wsRef?.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return undefined;
    const collection = activeCollection === 'generated'
      ? 'generated'
      : activeCollection === 'user'
        ? 'user'
        : 'all';
    const timer = setTimeout(() => {
      try {
        ws.send(JSON.stringify({
          type: 'MEDIA_SEARCH_REQUEST',
          schema_version: 1,
          request_id: `media_search_${Date.now()}`,
          payload: { query: q, collection, limit: 50 }
        }));
      } catch {
        // socket closed mid-send — the local filter remains the view
      }
    }, 250);
    return () => clearTimeout(timer);
  }, [wsRef, searchQuery, activeCollection]);

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
    // Collection filter (shared by the local and backend paths)
    const matchesCollection = (item) => {
      const origin = (item.origin || item.probe_metadata?.origin || '').toLowerCase();
      const collection = (item.collection || '').toLowerCase();

      if (activeCollection === 'generated') {
        return collection === 'generated' || origin === 'agent_generated';
      }
      if (activeCollection === 'snips') {
        return origin === 'snip';
      }
      if (activeCollection === 'user') {
        return collection === 'user' || origin === 'user_upload' || origin === 'snip';
      }
      return true;
    };

    const trimmedQuery = searchQuery.trim();

    // N07: when the backend recall has answered THIS query, its
    // results are the view (transcript + semantic matches the local
    // metadata filter can never see) — only the collection tab still
    // applies, the backend already matched the text.
    if (trimmedQuery && searchResults && searchResults.query === trimmedQuery) {
      return searchResults.items.filter(matchesCollection);
    }

    return effectiveMedia.filter(item => {
      if (!matchesCollection(item)) return false;

      // Search query — local metadata fallback (offline, or the
      // backend answer for this query has not landed yet)
      if (trimmedQuery) {
        const q = trimmedQuery.toLowerCase();
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
  }, [effectiveMedia, searchResults, activeCollection, searchQuery, getSessionTitle]);

  // Jump to conversation handler. Navigation (session switch + the
  // scroll to the exact message) is owned by the App-level media
  // navigation handler, which scrolls when the message renders —
  // N08 removed this component's timed DOM lookup, which raced the
  // session switch and usually found nothing.
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

    const disposition = sendMediaAction(
      'MEDIA_METADATA_UPDATE_REQUEST',
      { media_id: mediaId, tags, notes },
      `meta_update_${Date.now()}`
    );

    if (disposition !== 'queued') {
      // Sent (or a legacy standalone send): local optimistic update
      // as before — the MEDIA_METADATA_UPDATE_RESPONSE merge is
      // idempotent, so the reply simply confirms it. Queued: apply
      // NOTHING locally (RUX-A-09) — the card marks the edit as
      // queued from the real queue, and the response handler
      // applies the real values when the answer lands.
      setInternalMedia(prev => prev.map(m => {
        if (m.id === mediaId) {
          return { ...m, tags, user_notes: notes };
        }
        return m;
      }));
    }

    setSelectedMedia(null);
  };

  // Trigger delete prompt
  const handleDeletePrompt = (item, e) => {
    if (e) e.stopPropagation();
    // A delete for this item is already sitting in the offline
    // queue — a second one would only duplicate it on flush.
    if (pendingDeleteFor(item.id)) return;
    setDeleteTarget(item);
    setDeleteUnderlyingFile(false);
  };

  // Confirm delete
  const handleConfirmDelete = () => {
    if (!deleteTarget) return;
    const mid = deleteTarget.id;
    const delFile = deleteUnderlyingFile;

    // OP12: remember the request so the response handler can tell
    // "file deleted as asked" apart from "file silently kept". This
    // is recorded for queued deletes too — when the flushed request
    // is answered after reconnect, the judgement runs exactly as
    // for a live delete.
    pendingDeletesRef.current.set(mid, {
      item: deleteTarget,
      name: (deleteTarget.file_path || '').split(/[/\\]/).pop() || 'the file',
      deleteFile: delFile,
    });

    const disposition = sendMediaAction(
      'MEDIA_DELETE_REQUEST',
      { media_id: mid, delete_underlying_file: delFile },
      `media_delete_${Date.now()}`
    );

    if (disposition !== 'queued') {
      // Sent (or a legacy standalone send): optimistic removal as
      // before — the reply confirms it, or restores the entry with
      // a notice when the engine refused (OP12). Queued: the item
      // STAYS in the grid marked "Delete queued" (RUX-A-09) and is
      // removed only by the MEDIA_DELETE_RESPONSE handler when the
      // real answer lands — never by the click alone.
      setInternalMedia(prev => prev.filter(m => m.id !== mid));
      if (selectedMedia?.id === mid) setSelectedMedia(null);
    }

    setDeleteTarget(null);
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
            placeholder="Search filename, tags, notes, transcripts..."
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

      {/* N07: honest recall state — when the embedding endpoint was
          down, backend search degraded to lexical matches only. */}
      {searchQuery.trim() && searchResults && searchResults.query === searchQuery.trim() && searchResults.degraded && (
        <div
          data-testid="media-search-degraded"
          style={{ fontSize: '11px', color: '#fbbf24', marginBottom: '10px' }}
        >
          Semantic search is unavailable right now — showing text matches only.
        </div>
      )}

      {/* OP12: honest delete outcome — shown when a delete fell short
          of what was asked (entry refused, or the file the user chose
          to delete could not be deleted from disk). */}
      {deleteNotice && (
        <div
          data-testid="media-delete-notice"
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
            fontSize: '12px',
            color: deleteNotice.tone === 'error' ? '#f87171' : '#fbbf24',
            background: deleteNotice.tone === 'error'
              ? 'rgba(239, 68, 68, 0.1)'
              : 'rgba(251, 191, 36, 0.08)',
            border: deleteNotice.tone === 'error'
              ? '1px solid rgba(239, 68, 68, 0.3)'
              : '1px solid rgba(251, 191, 36, 0.3)',
            borderRadius: '8px',
            padding: '8px 12px',
            marginBottom: '10px'
          }}
        >
          <AlertTriangle size={14} style={{ flexShrink: 0 }} />
          <span style={{ flex: 1 }}>{deleteNotice.text}</span>
          <button
            type="button"
            data-testid="media-delete-notice-dismiss"
            onClick={() => setDeleteNotice(null)}
            style={{
              background: 'transparent',
              border: 'none',
              color: 'inherit',
              cursor: 'pointer',
              padding: '2px',
              display: 'flex',
              alignItems: 'center'
            }}
          >
            <X size={13} />
          </button>
        </div>
      )}

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
              // RUX-A-09: queued markings derive from the real
              // offline queue — they clear themselves on flush.
              const deleteQueued = Boolean(pendingDeleteFor(item.id));
              const metaQueued = Boolean(pendingMetaFor(item.id));

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

            {/* RUX-A-09: queued state for THIS item, derived from
                the real queue — an earlier offline save/delete is
                still waiting to land, so say so here too instead of
                letting a second edit stack onto the pending one. */}
            {Boolean(pendingDeleteFor(selectedMedia.id)) && (
              <div
                data-testid="media-detail-delete-queued"
                style={{ fontSize: '12px', color: '#fbbf24', background: 'rgba(251, 191, 36, 0.08)', border: '1px solid rgba(251, 191, 36, 0.3)', borderRadius: '8px', padding: '8px 12px' }}
              >
                Delete queued — this item will be removed when the engine reconnects.
              </div>
            )}
            {Boolean(pendingMetaFor(selectedMedia.id)) && (
              <div
                data-testid="media-detail-meta-queued"
                style={{ fontSize: '12px', color: '#fbbf24', background: 'rgba(251, 191, 36, 0.08)', border: '1px solid rgba(251, 191, 36, 0.3)', borderRadius: '8px', padding: '8px 12px' }}
              >
                An edit for this item is queued — it will apply when the engine reconnects.
              </div>
            )}

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
                disabled={Boolean(pendingMetaFor(selectedMedia.id))}
                style={{
                  background: '#8b5cf6',
                  border: 'none',
                  color: '#fff',
                  borderRadius: '8px',
                  padding: '8px 18px',
                  fontSize: '12px',
                  fontWeight: 600,
                  cursor: pendingMetaFor(selectedMedia.id) ? 'default' : 'pointer',
                  opacity: pendingMetaFor(selectedMedia.id) ? 0.55 : 1
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
              Remove{' '}
              <span style={{ color: '#e2e8f0', fontWeight: 600 }}>
                {(deleteTarget.file_path || '').split(/[/\\]/).pop() || 'this media item'}
              </span>{' '}
              from your library? The library entry is removed either way — the file itself
              stays in its folder unless you also delete it below.
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
              <span>Also delete the file from disk</span>
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
