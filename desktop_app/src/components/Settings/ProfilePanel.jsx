import React, { useState, useEffect } from 'react';
import { Trash2, Pencil, User, Users, SlidersHorizontal } from 'lucide-react';
import { useAetherSocket } from '../../contexts/SocketContext';
import LoadStateRow from './LoadStateRow';
import EmptyState from '../EmptyState';

// Profile as its own Settings section: what Aether knows about
// the user, the people in their life, and their preferences,
// read through the profile builder's shape — three sections
// (user / people / preferences), each paged on its own
// (limit + offset, with the section's total reported back).
// Every row carries the engine-supplied source label verbatim
// (no surface formats raw provenance itself), plus importance
// and confidence where the store recorded them. Edit / Delete
// reuse the existing fact actions — the same
// MEMORY_FACT_UPDATE_REQUEST / MEMORY_FACT_DELETE_REQUEST pairs
// the Memory panel's graph view uses; rows change only when the
// engine's reply confirms the change, and a refusal keeps the
// row and says why.

const PAGE_SIZE = 20;

const SECTION_DEFS = [
  { key: 'user', title: 'About you', icon: <User size={20} />, emptyTitle: 'Nothing about you yet', emptyText: 'Facts you state, and facts Aether learns about you, will appear here.' },
  { key: 'people', title: 'People', icon: <Users size={20} />, emptyTitle: 'No people yet', emptyText: 'People Aether knows about will appear here, grouped by person.' },
  { key: 'preferences', title: 'Preferences', icon: <SlidersHorizontal size={20} />, emptyTitle: 'No preferences yet', emptyText: 'Your stated and learned preferences will appear here.' },
];

// The profile read answers per section. The canonical section
// value is { items, total }; a bare array is accepted (its
// length is then the only total there is), and the people
// section is also accepted in the profile builder's native
// grouped shape ({ personName: [items] }), flattened with the
// group name as each item's entity.
function normalizeSection(raw) {
  if (Array.isArray(raw)) {
    return { items: raw, total: raw.length };
  }
  if (raw && Array.isArray(raw.items)) {
    return { items: raw.items, total: typeof raw.total === 'number' ? raw.total : raw.items.length };
  }
  if (raw && typeof raw === 'object') {
    const items = [];
    Object.entries(raw).forEach(([name, list]) => {
      if (Array.isArray(list)) {
        list.forEach((item) => items.push({ entity_name: name, ...item }));
      }
    });
    return { items, total: typeof raw.total === 'number' ? raw.total : items.length };
  }
  return { items: [], total: 0 };
}

export default function ProfilePanel() {
  const { wsRef, sendMessage, isConnected } = useAetherSocket();
  const [profileLoad, setProfileLoad] = useState('loading');
  const [profileError, setProfileError] = useState(null);
  const [sections, setSections] = useState({
    user: { items: [], total: 0 },
    people: { items: [], total: 0 },
    preferences: { items: [], total: 0 },
  });
  const [sectionLimits, setSectionLimits] = useState({ user: PAGE_SIZE, people: PAGE_SIZE, preferences: PAGE_SIZE });
  // The message listener is registered once (mount), so refresh
  // sends read the current limits from this ref — the state value
  // captured at mount would shrink a loaded-more section back to
  // its first page on the next refresh.
  const sectionLimitsRef = React.useRef(sectionLimits);
  const [editingFactId, setEditingFactId] = useState(null);
  const [editText, setEditText] = useState('');
  const [editError, setEditError] = useState(null);
  const [actionError, setActionError] = useState(null);

  const requestProfile = (limits) => {
    const effective = limits || sectionLimitsRef.current;
    sendMessage('MEMORY_PROFILE_REQUEST', {
      schema_version: 1,
      request_id: Date.now().toString(),
      payload: {
        sections: {
          user: { limit: effective.user, offset: 0 },
          people: { limit: effective.people, offset: 0 },
          preferences: { limit: effective.preferences, offset: 0 },
        },
      },
    });
  };

  useEffect(() => {
    requestProfile();

    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'MEMORY_PROFILE_RESPONSE') {
          if (data.payload.success === false || data.payload.error) {
            setProfileError(data.payload.error || 'Your profile could not be loaded.');
            setProfileLoad('error');
          } else {
            setProfileError(null);
            setSections({
              user: normalizeSection(data.payload.user),
              people: normalizeSection(data.payload.people),
              preferences: normalizeSection(data.payload.preferences),
            });
            setProfileLoad('ready');
          }
        } else if (data.type === 'MEMORY_FACT_UPDATE_RESPONSE') {
          if (data.payload.success) {
            setEditingFactId(null);
            setEditError(null);
            setActionError(null);
            requestProfile();
          } else {
            // Mirrors the Memory panel: a rejected edit keeps the
            // editor open and explains — the engine's reason when
            // it sent one, the plain statement when it did not.
            setEditError(data.payload.error || 'The update was rejected — the fact was not changed.');
          }
        } else if (data.type === 'MEMORY_FACT_DELETE_RESPONSE') {
          if (data.payload.success === false || data.payload.error) {
            setActionError(data.payload.error || 'The fact could not be deleted.');
          } else {
            setActionError(null);
            requestProfile();
          }
        }
      } catch (e) {
        // ignore
      }
    };

    if (wsRef.current) {
      wsRef.current.addEventListener('message', handleMessage);
    }
    return () => {
      if (wsRef.current) {
        wsRef.current.removeEventListener('message', handleMessage);
      }
    };
    // The fetch is mount-only plus the explicit load-more sends;
    // refresh-after-action reads the limits ref, not state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wsRef, sendMessage]);

  const handleLoadMore = (sectionKey) => {
    const next = { ...sectionLimits, [sectionKey]: sectionLimits[sectionKey] + PAGE_SIZE };
    setSectionLimits(next);
    sectionLimitsRef.current = next;
    requestProfile(next);
  };

  const handleStartEdit = (item) => {
    setEditingFactId(item.fact_id);
    setEditText(item.text ?? item.fact_text ?? item.content ?? '');
    setEditError(null);
  };

  const handleSaveEdit = (item) => {
    sendMessage('MEMORY_FACT_UPDATE_REQUEST', {
      schema_version: 1,
      request_id: Date.now().toString(),
      payload: { fact_id: item.fact_id, fact_text: editText },
    });
  };

  const handleDeleteFact = (item) => {
    if (!item?.fact_id) return;
    const text = item.text ?? item.fact_text ?? item.content ?? '';
    if (window.confirm(`Delete this fact from your profile?\n\n"${text}"`)) {
      sendMessage('MEMORY_FACT_DELETE_REQUEST', {
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { fact_id: item.fact_id },
      });
    }
  };

  const renderFactRow = (item) => {
    const isEditing = editingFactId === item.fact_id;
    const text = item.text ?? item.fact_text ?? item.content ?? '';
    const hasImportance = typeof item.importance === 'number' && Number.isFinite(item.importance);
    const hasConfidence = typeof item.confidence === 'number' && Number.isFinite(item.confidence);
    return (
      <div key={item.fact_id} data-testid={`profile-fact-${item.fact_id}`} style={{ display: 'flex', gap: '12px', alignItems: 'flex-start', background: 'var(--bg-base)', padding: '12px', borderRadius: '8px' }}>
        {isEditing ? (
          <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <textarea
              value={editText}
              onChange={(e) => setEditText(e.target.value)}
              rows={3}
              style={{ background: 'var(--bg-base)', border: '1px solid rgba(var(--accent-rgb),0.4)', borderRadius: '6px', color: 'var(--text-primary)', padding: '8px', fontSize: '13px', lineHeight: '1.5', resize: 'vertical', outline: 'none', fontFamily: 'inherit' }}
            />
            <div style={{ display: 'flex', gap: '8px' }}>
              <button
                onClick={() => handleSaveEdit(item)}
                style={{ background: 'rgba(var(--accent-rgb),0.35)', border: '1px solid rgba(var(--accent-rgb),0.5)', color: 'var(--text-primary)', padding: '5px 12px', borderRadius: '6px', fontSize: '12px', fontWeight: 500, cursor: 'pointer' }}
              >
                Save
              </button>
              <button
                onClick={() => { setEditingFactId(null); setEditError(null); }}
                style={{ background: 'var(--bg-hover)', border: '1px solid var(--border-default)', color: 'var(--text-secondary)', padding: '5px 12px', borderRadius: '6px', fontSize: '12px', cursor: 'pointer' }}
              >
                Cancel
              </button>
            </div>
            {editError && (
              <div style={{ color: 'var(--danger)', fontSize: '12px' }}>{editError}</div>
            )}
          </div>
        ) : (
          <>
            <div style={{ flex: 1, fontSize: '13px', lineHeight: '1.5' }}>
              {text}
              {item.source_label && (
                <span
                  data-testid={`profile-source-${item.fact_id}`}
                  style={{ marginLeft: '8px', fontSize: '10px', background: 'var(--bg-hover)', color: 'var(--text-secondary)', padding: '1px 6px', borderRadius: '8px', textTransform: 'uppercase', letterSpacing: '0.04em' }}
                >
                  {item.source_label}
                </span>
              )}
              {hasImportance && (
                <span data-testid={`profile-importance-${item.fact_id}`} style={{ marginLeft: '8px', fontSize: '11px', color: 'var(--text-muted)' }}>
                  importance {Math.round(item.importance * 100)}%
                </span>
              )}
              {hasConfidence && (
                <span data-testid={`profile-confidence-${item.fact_id}`} style={{ marginLeft: '8px', fontSize: '11px', color: 'var(--text-muted)' }}>
                  confidence {Math.round(item.confidence * 100)}%
                </span>
              )}
            </div>
            <button
              title="Edit this fact"
              onClick={() => handleStartEdit(item)}
              style={{ display: 'flex', alignItems: 'center', gap: '4px', background: 'transparent', border: '1px solid var(--border-default)', color: 'var(--text-secondary)', padding: '4px 8px', borderRadius: '6px', fontSize: '11px', cursor: 'pointer', flexShrink: 0, transition: 'all 0.2s' }}
              onMouseEnter={(e) => { e.currentTarget.style.color = 'var(--text-primary)'; e.currentTarget.style.borderColor = 'rgba(var(--accent-rgb),0.5)'; }}
              onMouseLeave={(e) => { e.currentTarget.style.color = 'var(--text-secondary)'; e.currentTarget.style.borderColor = 'var(--border-default)'; }}
            >
              <Pencil size={13} />
              Edit
            </button>
            <button
              title="Delete this fact"
              onClick={() => handleDeleteFact(item)}
              style={{ display: 'flex', alignItems: 'center', gap: '4px', background: 'transparent', border: '1px solid var(--border-default)', color: 'var(--text-secondary)', padding: '4px 8px', borderRadius: '6px', fontSize: '11px', cursor: 'pointer', flexShrink: 0, transition: 'all 0.2s' }}
              onMouseEnter={(e) => { e.currentTarget.style.color = 'var(--danger)'; e.currentTarget.style.borderColor = 'rgba(201,42,42,0.5)'; }}
              onMouseLeave={(e) => { e.currentTarget.style.color = 'var(--text-secondary)'; e.currentTarget.style.borderColor = 'var(--border-default)'; }}
            >
              <Trash2 size={13} />
              Delete
            </button>
          </>
        )}
      </div>
    );
  };

  const renderSection = (def) => {
    const section = sections[def.key];
    return (
      <div key={def.key} data-testid={`profile-section-${def.key}`} style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '20px', marginBottom: '24px' }}>
        <h3 style={{ fontSize: '16px', fontWeight: 500, margin: '0 0 16px 0' }}>{def.title}</h3>
        {profileLoad !== 'ready' ? (
          <LoadStateRow
            loadState={profileLoad}
            isConnected={isConnected}
            testId={`profile-${def.key}-load-state`}
            loadingText={`Loading ${def.title.toLowerCase()}…`}
            offlineText="Waiting for the engine — your profile will appear here once it connects."
            errorText="Your profile could not be loaded."
          />
        ) : section.items.length === 0 ? (
          <EmptyState
            compact
            icon={def.icon}
            title={def.emptyTitle}
            description={def.emptyText}
            testId={`profile-${def.key}-empty`}
          />
        ) : (
          <>
            {def.key === 'people' ? (
              Object.entries(section.items.reduce((acc, item) => {
                const name = item.entity_name || 'Someone';
                acc[name] = acc[name] || [];
                acc[name].push(item);
                return acc;
              }, {})).map(([name, items]) => (
                <div key={name} style={{ marginBottom: '16px' }}>
                  <div style={{ fontSize: '13px', fontWeight: 500, color: 'var(--accent)', marginBottom: '8px' }}>{name}</div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                    {items.map(renderFactRow)}
                  </div>
                </div>
              ))
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                {section.items.map(renderFactRow)}
              </div>
            )}
            {section.items.length < section.total && (
              <button
                data-testid={`profile-${def.key}-more`}
                onClick={() => handleLoadMore(def.key)}
                style={{ marginTop: '12px', background: 'var(--bg-hover)', border: '1px solid var(--border-default)', color: 'var(--text-secondary)', padding: '6px 14px', borderRadius: '8px', fontSize: '12px', cursor: 'pointer' }}
              >
                Show more ({section.total - section.items.length} more)
              </button>
            )}
          </>
        )}
      </div>
    );
  };

  return (
    <div style={{ color: 'var(--text-primary)' }}>
      <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Profile</h2>
      <p style={{ color: 'var(--text-muted)', fontSize: '14px', marginBottom: '24px' }}>
        What Aether knows about you, the people in your life, and your preferences. Every entry can be corrected or removed — changes take effect in the memory store itself.
      </p>
      {actionError && (
        <div style={{ color: 'var(--danger)', fontSize: '13px', marginBottom: '16px' }}>{actionError}</div>
      )}
      {SECTION_DEFS.map(renderSection)}
    </div>
  );
}
