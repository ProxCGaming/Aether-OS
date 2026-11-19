import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { Network, Search } from 'lucide-react';
import { useAetherSocket } from '../../contexts/SocketContext';
import LoadStateRow from './LoadStateRow';
import EmptyState from '../EmptyState';

// Memory graph neighbourhood: one entity at the centre, the
// entities it appears together with around it. Edges are DERIVED
// at read time by the engine — a link means the two entities
// co-occur in the same fact or episode, counted; nothing here is
// an extracted relation, and the panel says so in plain words.
//
// Self-contained: by default the entity picker is fed from the
// same MEMORY_GRAPH_REQUEST fact list the Memory panel already
// uses (distinct entity names off the facts). A parent that
// already holds the entity list can pass `entities`
// ([{entity_name, entity_type, fact_count}]) and no fetch happens.
// `onOpenEntity(entityName)`, when given, adds a "View facts"
// action to the centre card so the host can route to its own fact
// surface; without it the centre card shows the fact count only.

const chipStyle = {
  display: 'inline-block',
  fontSize: '11px',
  padding: '2px 8px',
  borderRadius: '999px',
  border: '1px solid var(--border-strong)',
  color: 'var(--text-secondary)',
};

export default function GraphNeighbourhood({
  entities: entitiesProp = null,
  initialEntity = '',
  onOpenEntity = null,
}) {
  const { wsRef, sendMessage, isConnected } = useAetherSocket();
  const [fetchedEntities, setFetchedEntities] = useState([]);
  const [entitiesLoad, setEntitiesLoad] = useState(entitiesProp ? 'ready' : 'loading');
  const [entitiesError, setEntitiesError] = useState('');
  const [pickerQuery, setPickerQuery] = useState('');
  const [centreName, setCentreName] = useState('');
  const [depth, setDepth] = useState(1);
  const [hood, setHood] = useState(null);
  const [hoodLoad, setHoodLoad] = useState('idle');
  const [hoodError, setHoodError] = useState('');

  const entities = useMemo(() => {
    if (entitiesProp) return entitiesProp;
    return fetchedEntities;
  }, [entitiesProp, fetchedEntities]);

  const visibleEntities = useMemo(() => {
    const q = pickerQuery.trim().toLowerCase();
    if (!q) return entities;
    return entities.filter((e) => e.entity_name.toLowerCase().includes(q));
  }, [entities, pickerQuery]);

  const requestNeighbourhood = useCallback((name, d) => {
    setCentreName(name);
    setHood(null);
    setHoodError('');
    setHoodLoad('loading');
    sendMessage('MEMORY_GRAPH_NEIGHBOURHOOD_REQUEST', {
      schema_version: 1,
      request_id: Date.now().toString(),
      payload: { entity_name: name, depth: d },
    });
  }, [sendMessage]);

  useEffect(() => {
    if (!entitiesProp) {
      sendMessage('MEMORY_GRAPH_REQUEST', {
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {},
      });
    }
    if (initialEntity) {
      requestNeighbourhood(initialEntity, 1);
    }
    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'MEMORY_GRAPH_RESPONSE' && !entitiesProp) {
          const payload = data.payload || {};
          if (payload.success === false || payload.error) {
            setEntitiesError(payload.error || 'The entity list could not be loaded.');
            setEntitiesLoad('error');
          } else {
            // Distinct entities off the fact list: name, the
            // first-seen type, and how many facts mention it.
            const byName = new Map();
            (payload.facts || []).forEach((f) => {
              if (!f || !f.entity_name) return;
              const existing = byName.get(f.entity_name);
              if (existing) {
                existing.fact_count += 1;
              } else {
                byName.set(f.entity_name, {
                  entity_name: f.entity_name,
                  entity_type: f.entity_type || '',
                  fact_count: 1,
                });
              }
            });
            setFetchedEntities(
              [...byName.values()].sort((a, b) => a.entity_name.localeCompare(b.entity_name)),
            );
            setEntitiesError('');
            setEntitiesLoad('ready');
          }
        } else if (data.type === 'MEMORY_GRAPH_NEIGHBOURHOOD_RESPONSE') {
          const payload = data.payload || {};
          if (payload.success === false || payload.error) {
            setHoodError(payload.error || 'That neighbourhood could not be loaded.');
            setHoodLoad('error');
          } else {
            setHood(payload);
            setHoodError('');
            setHoodLoad('ready');
          }
        }
      } catch {
        // ignore malformed frames, same as the other panels
      }
    };
    const ws = wsRef.current;
    if (ws) ws.addEventListener('message', handleMessage);
    return () => { if (ws) ws.removeEventListener('message', handleMessage); };
    // Mount-only: the requests above are the panel's opening reads.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wsRef]);

  const changeDepth = (d) => {
    setDepth(d);
    if (centreName) requestNeighbourhood(centreName, d);
  };

  const centre = hood && hood.centre ? hood.centre : null;
  const neighbours = (hood && hood.neighbours) || [];
  const chains = (hood && hood.replaced_chains) || [];
  // The engine answers an unknown entity with an honest empty
  // result naming it — a centre with no records at all (or no
  // centre) is that answer, not an error.
  const centreIsUnknown = hoodLoad === 'ready'
    && (!centre || ((centre.fact_count ?? 0) === 0 && (centre.episode_count ?? 0) === 0));

  return (
    <div style={{ color: 'var(--text-primary)', display: 'flex', flexDirection: 'column', gap: '16px' }}>
      <div>
        <h3 style={{ fontSize: '16px', fontWeight: 500, display: 'flex', alignItems: 'center', gap: '8px', margin: '0 0 6px' }}>
          <Network size={18} /> Graph neighbourhood
        </h3>
        <p style={{ color: 'var(--text-muted)', fontSize: '13px', margin: 0, lineHeight: 1.5 }}>
          Pick an entity to see who and what appears together with it in your memory.
          A link only means the two appeared together in the same fact or episode —
          links are counted from co-appearances, not extracted relations.
        </p>
      </div>

      <div style={{ position: 'relative' }}>
        <Search size={15} style={{ position: 'absolute', left: '10px', top: '10px', color: 'var(--text-muted)' }} />
        <input
          data-testid="graph-entity-search"
          type="text"
          value={pickerQuery}
          onChange={(e) => setPickerQuery(e.target.value)}
          placeholder="Search entities…"
          style={{ width: '100%', boxSizing: 'border-box', paddingLeft: '32px' }}
        />
      </div>

      {entitiesLoad !== 'ready' ? (
        <LoadStateRow
          loadState={entitiesLoad}
          isConnected={isConnected}
          loadingText="Loading the entities in your memory…"
          offlineText="Waiting for the engine — the entity list will appear when it reconnects."
          errorText={entitiesError || 'The entity list could not be loaded.'}
          testId="graph-entities-load-state"
        />
      ) : (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px', maxHeight: '132px', overflowY: 'auto' }}>
          {visibleEntities.length === 0 && (
            <div style={{ color: 'var(--text-secondary)', fontSize: '13px' }}>
              {entities.length === 0
                ? 'No entities in memory yet — facts you save will name the people and places here.'
                : `No entities match “${pickerQuery.trim()}”.`}
            </div>
          )}
          {visibleEntities.map((e) => (
            <button
              key={e.entity_name}
              type="button"
              data-testid={`graph-entity-option-${e.entity_name}`}
              onClick={() => requestNeighbourhood(e.entity_name, depth)}
              style={{
                background: e.entity_name === centreName ? 'rgba(var(--accent-rgb), 0.15)' : 'var(--bg-base)',
                color: 'var(--text-primary)',
                border: `1px solid ${e.entity_name === centreName ? 'var(--accent)' : 'var(--border-default)'}`,
                borderRadius: '999px',
                padding: '6px 12px',
                fontSize: '13px',
                cursor: 'pointer',
              }}
            >
              {e.entity_name}
              {e.entity_type ? <span style={{ color: 'var(--text-muted)' }}> · {e.entity_type}</span> : null}
              {typeof e.fact_count === 'number' ? <span style={{ color: 'var(--text-muted)' }}> · {e.fact_count} facts</span> : null}
            </button>
          ))}
        </div>
      )}

      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
        <span style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>Depth</span>
        {[1, 2].map((d) => (
          <button
            key={d}
            type="button"
            data-testid={`graph-depth-${d}`}
            onClick={() => changeDepth(d)}
            style={{
              background: depth === d ? 'rgba(var(--accent-rgb), 0.15)' : 'var(--bg-base)',
              color: 'var(--text-primary)',
              border: `1px solid ${depth === d ? 'var(--accent)' : 'var(--border-default)'}`,
              borderRadius: '8px',
              padding: '5px 14px',
              fontSize: '13px',
              cursor: 'pointer',
            }}
          >
            {d}
          </button>
        ))}
      </div>

      {hoodLoad === 'idle' && (
        <EmptyState
          compact
          testId="graph-no-centre"
          title="No entity picked yet"
          description="Pick an entity above and its neighbourhood appears here."
        />
      )}
      {hoodLoad === 'loading' && (
        <LoadStateRow
          loadState="loading"
          isConnected={isConnected}
          loadingText={`Loading ${centreName}’s neighbourhood…`}
          offlineText="Waiting for the engine — the neighbourhood will appear when it reconnects."
          testId="graph-hood-load-state"
        />
      )}
      {hoodLoad === 'error' && (
        <LoadStateRow
          loadState="error"
          isConnected={isConnected}
          errorText={hoodError || 'That neighbourhood could not be loaded.'}
          testId="graph-hood-load-state"
        />
      )}
      {hoodLoad === 'ready' && centreIsUnknown && (
        <EmptyState
          testId="graph-unknown-entity"
          title={`Nothing is known about “${(centre && centre.entity_name) || centreName}” yet`}
          description="No facts or episodes mention this entity, so there is no neighbourhood to show."
        />
      )}
      {hoodLoad === 'ready' && !centreIsUnknown && centre && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
          <div data-testid="graph-centre-card" style={{ background: 'var(--bg-base)', border: '1px solid var(--border-default)', borderRadius: '12px', padding: '16px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '12px' }}>
              <div>
                <div style={{ fontSize: '16px', fontWeight: 600 }}>{centre.entity_name}</div>
                <div style={{ marginTop: '6px', display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
                  {centre.entity_type ? <span style={chipStyle}>{centre.entity_type}</span> : null}
                  <span style={chipStyle}>{centre.fact_count ?? 0} facts</span>
                  <span style={chipStyle}>{centre.episode_count ?? 0} episodes</span>
                </div>
              </div>
              {onOpenEntity && (
                <button
                  type="button"
                  data-testid="graph-centre-facts"
                  onClick={() => onOpenEntity(centre.entity_name)}
                  style={{ background: 'var(--bg-hover)', color: 'var(--text-primary)', border: '1px solid var(--border-strong)', borderRadius: '8px', padding: '6px 12px', fontSize: '12px', cursor: 'pointer', flexShrink: 0 }}
                >
                  View facts
                </button>
              )}
            </div>
          </div>

          {neighbours.length === 0 ? (
            <div style={{ color: 'var(--text-secondary)', fontSize: '13px' }}>
              No other entities appear together with {centre.entity_name} yet.
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              {neighbours.map((n) => (
                <button
                  key={n.entity_name}
                  type="button"
                  data-testid={`graph-neighbour-${n.entity_name}`}
                  onClick={() => requestNeighbourhood(n.entity_name, depth)}
                  style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '12px', background: 'var(--bg-base)', border: '1px solid var(--border-default)', borderRadius: '10px', padding: '10px 14px', cursor: 'pointer', textAlign: 'left', color: 'var(--text-primary)' }}
                >
                  <span style={{ fontSize: '14px' }}>
                    {n.entity_name}
                    {n.entity_type ? <span style={{ color: 'var(--text-muted)' }}> · {n.entity_type}</span> : null}
                  </span>
                  <span style={{ display: 'flex', gap: '6px', flexShrink: 0 }}>
                    <span style={{ ...chipStyle, borderColor: 'rgba(var(--accent-rgb),0.4)', color: 'var(--accent)' }}>
                      ×{n.cooccurrence_count} together
                    </span>
                    <span style={chipStyle}>{n.fact_count} facts</span>
                  </span>
                </button>
              ))}
            </div>
          )}

          {chains.length > 0 && (
            <div data-testid="graph-replaced-chains" style={{ background: 'var(--bg-base)', border: '1px solid var(--border-default)', borderRadius: '12px', padding: '14px 16px' }}>
              <div style={{ fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '8px' }}>Replaced facts</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                {chains.map((chain, i) => (
                  <div key={chain.fact_id || i} style={{ fontSize: '13px', display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
                    <button
                      type="button"
                      data-testid={`graph-chain-old-${i}`}
                      onClick={() => requestNeighbourhood(chain.old_entity, depth)}
                      style={{ background: 'none', border: 'none', color: 'var(--accent)', cursor: 'pointer', fontSize: '13px', padding: 0 }}
                    >
                      {chain.old_entity}
                    </button>
                    <span style={{ color: 'var(--text-muted)' }}>was replaced by</span>
                    <button
                      type="button"
                      data-testid={`graph-chain-new-${i}`}
                      onClick={() => requestNeighbourhood(chain.new_entity, depth)}
                      style={{ background: 'none', border: 'none', color: 'var(--accent)', cursor: 'pointer', fontSize: '13px', padding: 0 }}
                    >
                      {chain.new_entity}
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
