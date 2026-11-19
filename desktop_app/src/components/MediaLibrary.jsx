import React, { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import {
  Search,
  Image as ImageIcon,
  Crop,
  Sparkles,
  Upload,
  AlertTriangle,
  Trash2,
  X,
  HardDrive
} from 'lucide-react';
import '../chat.css';
import LoadStateRow from './Settings/LoadStateRow';
import MediaGrid from './MediaGrid';
import MediaDetailModal from './MediaDetailModal';

// D3-03: a list request the engine never answers must not leave
// the surface on "loading" for the whole visit. Mirrors
// Onboarding's SAVE_NO_ANSWER_MS rule (which mirrors
// ProviderSettings' RUX-C-03): the request arms a timer, and if
// no answer lands within the window the load resolves to 'error'.
const LIST_NO_ANSWER_MS = 35000;

export default function MediaLibrary({
  media: controlledMedia,
  initialMedia,
  sessions = [],
  wsRef,
  safeSend = null,
  pendingMessages = [],
  reconnectCount = 0,
  isConnected = null,
  onNavigateToSession = () => {}
}) {
  const [internalMedia, setInternalMedia] = useState(() => initialMedia || []);
  // RUX-C-09: "No media items yet" rendered from the first frame —
  // during the list fetch and through any outage. Controlled/seeded
  // mounts hold real data and start ready; the fetched list starts
  // loading, and only an answered list may render the empty state.
  const [listLoad, setListLoad] = useState(
    () => (controlledMedia || initialMedia ? 'ready' : 'loading')
  );
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
  // D3-02: the message handlers need the selected item's ID, not
  // the item — reading it through this ref keeps selectedMedia out
  // of the subscription effect's deps, so opening/closing the
  // detail modal no longer tears down the listener and re-fetches
  // the whole list.
  const selectedMediaIdRef = useRef(null);
  useEffect(() => {
    selectedMediaIdRef.current = selectedMedia?.id ?? null;
  }, [selectedMedia]);
  // D3-07: items whose file URL failed to load fall back to the
  // icon placeholder (the file may have moved since the list was
  // answered) instead of a broken-image box.
  const [failedThumbs, setFailedThumbs] = useState(() => new Set());
  const markThumbFailed = useCallback((id) => {
    setFailedThumbs((prev) => {
      if (prev.has(id)) return prev;
      const next = new Set(prev);
      next.add(id);
      return next;
    });
  }, []);
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
          if (data.payload?.error) {
            setListLoad('error');
          } else if (Array.isArray(data.payload?.media)) {
            setListLoad('ready');
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
              if (selectedMediaIdRef.current === deletedId) setSelectedMedia(null);
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
            if (selectedMediaIdRef.current === progressId) {
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
            if (selectedMediaIdRef.current === analyzed.id) {
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
            if (selectedMediaIdRef.current === updatedId) {
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
    let noAnswerTimer = null;
    if (!controlledMedia && !initialMedia && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: 'MEDIA_LIST_REQUEST',
        schema_version: 1,
        request_id: `media_list_${Date.now()}`,
        payload: { collection: 'all' }
      }));
      // D3-03: arm the no-answer deadline for THIS request. The
      // state guard keeps a late timer from disturbing a list that
      // already resolved (ready, or an error the reply reported).
      noAnswerTimer = setTimeout(() => {
        setListLoad((s) => (s === 'loading' ? 'error' : s));
      }, LIST_NO_ANSWER_MS);
    }

    return () => {
      ws.removeEventListener('message', handleMessage);
      if (noAnswerTimer) clearTimeout(noAnswerTimer);
    };
  }, [wsRef, controlledMedia, initialMedia, reconnectCount]);

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
    // D3-25: an origin the library does not recognise is labelled
    // Unknown — the old fallthrough called it 'User upload'
    // (fabricating a provenance for any unrecognised source).
    return 'Unknown';
  };

  const getOriginBadgeStyle = (origin, collection) => {
    const norm = (origin || '').toLowerCase();
    if (norm === 'snip') {
      return {
        background: 'rgba(var(--accent-rgb), 0.18)',
        color: 'var(--accent)',
        border: '1px solid rgba(var(--accent-rgb), 0.35)'
      };
    }
    if (norm === 'agent_generated' || collection === 'generated') {
      return {
        background: 'rgba(76, 195, 138, 0.18)',
        color: 'var(--success)',
        border: '1px solid rgba(76, 195, 138, 0.35)'
      };
    }
    return {
      background: 'var(--bg-hover)',
      color: 'var(--text-secondary)',
      border: '1px solid var(--border-strong)'
    };
  };

  return (
    <div
      data-testid="media-library-view"
      style={{
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        color: 'var(--text-primary)',
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
            background: 'var(--bg-base)',
            border: '1px solid var(--border-default)',
            borderRadius: 'var(--radius-md)',
            padding: '6px 12px',
            gap: '8px',
            width: '320px',
            maxWidth: '100%'
          }}
        >
          <Search size={15} color="var(--text-muted)" />
          <input
            type="text"
            className="aether-search-input"
            data-testid="media-search-input"
            placeholder="Search filename, tags, notes, transcripts..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            style={{
              background: 'transparent',
              border: 'none',
              color: 'var(--text-primary)',
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
                color: 'var(--text-muted)',
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
              background: 'rgba(var(--accent-rgb), 0.18)',
              border: '1px solid rgba(var(--accent-rgb), 0.4)',
              color: 'var(--accent)',
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
                  background: isActive ? 'var(--bg-overlay)' : 'var(--bg-raised)',
                  border: isActive ? '1px solid var(--accent)' : '1px solid var(--border-default)',
                  color: isActive ? 'var(--accent)' : 'var(--text-muted)',
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
          style={{ fontSize: '11px', color: 'var(--accent)', marginBottom: '10px' }}
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
            color: deleteNotice.tone === 'error' ? 'var(--danger)' : 'var(--accent)',
            background: deleteNotice.tone === 'error'
              ? 'rgba(201, 42, 42, 0.1)'
              : 'rgba(var(--accent-rgb), 0.08)',
            border: deleteNotice.tone === 'error'
              ? '1px solid rgba(201, 42, 42, 0.3)'
              : '1px solid rgba(var(--accent-rgb), 0.3)',
            borderRadius: 'var(--radius-md)',
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
        {listLoad !== 'ready' && effectiveMedia.length === 0 ? (
          <div style={{ padding: '24px' }}>
            <LoadStateRow
              loadState={listLoad}
              isConnected={isConnected}
              testId="media-list-load-state"
              loadingText="Loading media…"
              offlineText="Waiting for the engine — your media will appear here once it connects."
              errorText="Media could not be loaded."
            />
          </div>
        ) : effectiveMedia.length === 0 ? (
          <div
            data-testid="empty-media-state"
            style={{
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'center',
              height: '320px',
              color: 'var(--text-muted)',
              gap: '12px'
            }}
          >
            <div
              style={{
                width: '56px',
                height: '56px',
                borderRadius: '50%',
                background: 'var(--bg-raised)',
                border: '1px solid var(--border-default)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center'
              }}
            >
              <ImageIcon size={26} color="var(--text-muted)" />
            </div>
            <div style={{ fontSize: '15px', fontWeight: 500, color: 'var(--text-primary)' }}>No media items yet</div>
            <div style={{ fontSize: '12px', color: 'var(--text-muted)', maxWidth: '360px', textAlign: 'center' }}>
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
              color: 'var(--text-muted)',
              gap: '10px'
            }}
          >
            <Search size={28} color="var(--text-muted)" />
            <div style={{ fontSize: '14px', fontWeight: 500, color: 'var(--text-primary)' }}>No matching media found</div>
            <div style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
              No media matches &quot;{searchQuery}&quot; in the {activeCollection} collection.
            </div>
          </div>
        ) : (
          <MediaGrid
            items={filteredMedia}
            getSessionTitle={getSessionTitle}
            failedThumbs={failedThumbs}
            onThumbError={markThumbFailed}
            onOpenDetail={handleOpenDetail}
            onDeletePrompt={handleDeletePrompt}
            onJumpToChat={handleJumpToChat}
            pendingDeleteFor={pendingDeleteFor}
            pendingMetaFor={pendingMetaFor}
            getOriginLabel={getOriginLabel}
            getOriginBadgeStyle={getOriginBadgeStyle}
          />
        )}
      </div>

      {/* Detail & Metadata Edit Modal — split out to
          MediaDetailModal (Wave 5); the library keeps the edit
          state and handlers and passes them down. */}
      {selectedMedia && (
        <MediaDetailModal
          item={selectedMedia}
          sessionTitle={getSessionTitle(selectedMedia.session_id)}
          editTags={editTags}
          tagInput={tagInput}
          editNotes={editNotes}
          thumbFailed={failedThumbs.has(selectedMedia.id)}
          deleteQueued={Boolean(pendingDeleteFor(selectedMedia.id))}
          metaQueued={Boolean(pendingMetaFor(selectedMedia.id))}
          getOriginLabel={getOriginLabel}
          getOriginBadgeStyle={getOriginBadgeStyle}
          onClose={() => setSelectedMedia(null)}
          onSave={handleSaveMetadata}
          onJumpToChat={handleJumpToChat}
          onAddTag={handleAddTag}
          onRemoveTag={handleRemoveTag}
          onTagInputChange={setTagInput}
          onNotesChange={setEditNotes}
          onThumbError={markThumbFailed}
        />
      )}
      {/* Delete Confirmation Modal */}
      {deleteTarget && (
        <div
          data-testid="delete-confirm-modal"
          className="aether-scrim"
          style={{
            position: 'fixed',
            inset: 0,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 1100,
          }}
          onClick={() => setDeleteTarget(null)}
        >
          <div
            className="aether-glass"
            style={{
              width: '420px',
              maxWidth: '90vw',
              padding: '24px',
              display: 'flex',
              flexDirection: 'column',
              gap: '16px',
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', color: 'var(--danger)' }}>
              <Trash2 size={22} />
              <h3 style={{ margin: 0, fontSize: '16px', color: 'var(--text-primary)' }}>Remove Media Entry</h3>
            </div>
            <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-muted)', lineHeight: 1.5 }}>
              Remove{' '}
              <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>
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
                color: 'var(--text-secondary)',
                cursor: 'pointer',
                background: 'var(--bg-raised)',
                padding: '8px 12px',
                borderRadius: 'var(--radius-md)'
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
                data-testid="confirm-delete-action-btn"
                onClick={handleConfirmDelete}
                style={{
                  background: 'var(--danger)',
                  border: 'none',
                  color: 'var(--text-primary)',
                  borderRadius: 'var(--radius-md)',
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
