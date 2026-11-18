import React, { useState, useEffect, useRef } from 'react';
import { Search } from 'lucide-react';
import { useAetherSocket } from '../../contexts/SocketContext';
import LoadStateRow from './LoadStateRow';
import EmptyState from '../EmptyState';
import CapabilityFilterBar from './CapabilityFilterBar';
import CapabilityItem from './CapabilityItem';
import SkillInstallCard from './SkillInstallCard';
import { capabilityFilterCounts, filterCapabilityItems } from './capabilityFilter';

// Wave 5 (Annex C row 7): the Skills tab, extracted from
// SettingsPanel.jsx — the shell keeps the skill list state and
// the toggle send; this panel is the controlled rendering.
//
// D4-1: the list runs through the shared capability pattern —
// CapabilityFilterBar + capabilityFilter for search/chips,
// CapabilityItem for the rows. A load_error skill classifies as
// the 'error' state: the Error chip finds it, and it is never
// dropped from the All list.
export default function SkillsPanel({ skills, skillsAnswered, isConnected, pendingSkillToggleFor, onSkillToggle }) {
  // Wave 5 (D4-6): the install/search traffic is this panel's
  // own — the skill LIST and the toggle stay shell-owned (props
  // above); only the registry search/prepare/install flow runs
  // on the panel's socket, the PluginsPanel pattern.
  const { wsRef, sendMessage } = useAetherSocket();
  const [capQuery, setCapQuery] = useState('');
  const [capFilter, setCapFilter] = useState('all');
  const entries = skills.map((skill) => ({
    item: skill,
    name: skill.name,
    state: skill.state === 'load_error' ? 'error' : (skill.enabled === false ? 'disabled' : 'enabled'),
    // FF4-04 skills half: a pinned skill whose files no longer
    // match its pin is the skills side of the Updates chip.
    hasUpdate: skill.pin?.status === 'drifted',
  }));
  const counts = capabilityFilterCounts(entries);
  const visibleEntries = filterCapabilityItems(entries, capQuery, capFilter);
  const clearCapabilityFilter = () => { setCapQuery(''); setCapFilter('all'); };

  // D4-6 install flow state. The source is always NAMED by the
  // user — there is no default registry to search, and the
  // engine's answer about a source (including "this source type
  // cannot be reached") is shown verbatim.
  const [sourceType, setSourceType] = useState('local');
  const [sourceLocator, setSourceLocator] = useState('');
  const [searchText, setSearchText] = useState('');
  const [searchResults, setSearchResults] = useState(null);
  const [searchError, setSearchError] = useState(null);
  const [searching, setSearching] = useState(false);
  const [pendingPin, setPendingPin] = useState(null);
  const [installError, setInstallError] = useState(null);
  const [installNotice, setInstallNotice] = useState(null);
  const seqRef = useRef(0);
  const searchReqRef = useRef(null);
  const prepareReqRef = useRef(null);
  const installReqRef = useRef(null);
  const mintRequestId = (kind) => {
    seqRef.current += 1;
    return `skills-${kind}-${Date.now()}-${seqRef.current}`;
  };

  useEffect(() => {
    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'SKILL_SEARCH_RESPONSE' && data.request_id === searchReqRef.current) {
          setSearching(false);
          if (data.payload.success) {
            setSearchResults(Array.isArray(data.payload.skills) ? data.payload.skills : []);
            setSearchError(null);
          } else {
            setSearchResults(null);
            setSearchError(data.payload.error || 'The search failed.');
          }
        } else if (data.type === 'SKILL_INSTALL_PREPARE_RESPONSE' && data.request_id === prepareReqRef.current) {
          if (data.payload.success && data.payload.pin) {
            setPendingPin(data.payload.pin);
            setInstallError(null);
          } else {
            setInstallError(data.payload.error || 'The skill could not be prepared.');
          }
        } else if (data.type === 'SKILL_INSTALL_RESPONSE' && data.request_id === installReqRef.current) {
          if (data.payload.success) {
            setPendingPin(null);
            setInstallError(null);
            setInstallNotice(
              data.payload.action === 'installed'
                ? `Installed "${data.payload.name}" — it is disabled. Enable it from the list below when you are ready.`
                : `"${data.payload.name}" was already installed; nothing changed.`,
            );
            // Refresh the shell-owned list so the new row (with
            // its pin annotation) appears.
            sendMessage('SKILL_LIST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
          } else {
            setInstallError(data.payload.error || 'The install failed.');
          }
        }
      } catch {
        // ignore
      }
    };
    const ws = wsRef.current;
    if (ws) {
      ws.addEventListener('message', handleMessage);
    }
    return () => {
      if (ws) {
        ws.removeEventListener('message', handleMessage);
      }
    };
  }, [wsRef, sendMessage]);

  const runSearch = () => {
    if (!sourceLocator.trim()) return;
    const requestId = mintRequestId('search');
    searchReqRef.current = requestId;
    setSearching(true);
    setSearchError(null);
    setInstallNotice(null);
    sendMessage('SKILL_SEARCH_REQUEST', {
      schema_version: 1,
      request_id: requestId,
      payload: { source_type: sourceType, locator: sourceLocator.trim(), query: searchText },
    });
  };

  const prepareInstall = (entry) => {
    const requestId = mintRequestId('prepare');
    prepareReqRef.current = requestId;
    setInstallError(null);
    sendMessage('SKILL_INSTALL_PREPARE_REQUEST', {
      schema_version: 1,
      request_id: requestId,
      payload: {
        source_type: sourceType,
        locator: sourceLocator.trim(),
        name: entry.name,
        ...(entry.path ? { skill_path: entry.path } : {}),
      },
    });
  };

  const confirmInstall = () => {
    if (!pendingPin) return;
    const requestId = mintRequestId('install');
    installReqRef.current = requestId;
    sendMessage('SKILL_INSTALL_REQUEST', {
      schema_version: 1,
      request_id: requestId,
      payload: {
        source_type: pendingPin.source_type,
        locator: pendingPin.source,
        name: pendingPin.name,
        expected_hash: pendingPin.hash,
      },
    });
  };

  return (
    <div style={{ color: 'var(--text-primary)' }}>
      <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Skills</h2>
      <p style={{ color: 'var(--text-muted)', fontSize: '14px', marginBottom: '24px' }}>
        Dynamic behaviors, prompts, and tool requirements loaded from `.agents/skills`. Trigger these in chat with slash commands.
      </p>

      {/* D4-6: search a source the user names — a local folder of
          skills, or a GitHub repository. There is deliberately no
          default registry: the engine answers for the named
          source only, and its answer is shown as-is. */}
      <div style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '20px', marginBottom: '24px' }}>
        <h3 style={{ fontSize: '16px', marginBottom: '4px' }}>Find &amp; Install Skills</h3>
        <p style={{ color: 'var(--text-muted)', fontSize: '13px', margin: '0 0 12px 0' }}>
          Search a source you name. Installing records a pin of the exact files you reviewed,
          and the skill starts out disabled.
        </p>
        <div style={{ display: 'flex', gap: '12px', flexWrap: 'wrap' }}>
          <select
            aria-label="Skill source type"
            value={sourceType}
            onChange={(e) => setSourceType(e.target.value)}
            style={{ padding: '10px 12px', borderRadius: '10px', background: 'var(--bg-base)', color: 'var(--text-primary)', border: '1px solid var(--border-default)' }}
          >
            <option value="local">Local directory</option>
            <option value="github">GitHub</option>
          </select>
          <input
            type="text"
            value={sourceLocator}
            onChange={(e) => setSourceLocator(e.target.value)}
            className="ios-glass-input"
            placeholder={sourceType === 'github' ? 'owner/repo' : 'Path to a folder of skills'}
            style={{ flex: '2 1 220px', padding: '10px 14px', color: 'var(--text-primary)', outline: 'none' }}
          />
          <input
            type="text"
            value={searchText}
            onChange={(e) => setSearchText(e.target.value)}
            className="ios-glass-input"
            placeholder="Filter by name or description (optional)"
            style={{ flex: '1 1 180px', padding: '10px 14px', color: 'var(--text-primary)', outline: 'none' }}
          />
          <button
            onClick={runSearch}
            disabled={!sourceLocator.trim() || searching}
            style={{ background: 'var(--accent)', color: 'var(--accent-on)', border: 'none', padding: '0 20px', borderRadius: '10px', cursor: 'pointer', fontWeight: 500, opacity: (!sourceLocator.trim() || searching) ? 0.55 : 1 }}
          >
            {searching ? 'Searching…' : 'Search'}
          </button>
        </div>
        {searchError && (
          <p data-testid="skill-search-error" style={{ color: 'var(--danger)', fontSize: '13px', margin: '12px 0 0 0' }}>
            {searchError}
          </p>
        )}
        {searchResults !== null && (
          <div style={{ marginTop: '16px', display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {searchResults.length === 0 ? (
              <p style={{ color: 'var(--text-secondary)', fontSize: '13px', margin: 0 }}>
                No skills in that source match.
              </p>
            ) : searchResults.map((entry) => (
              <div
                key={`${entry.name}-${entry.path || ''}`}
                style={{ display: 'flex', alignItems: 'center', gap: '12px', background: 'var(--bg-base)', borderRadius: '10px', padding: '10px 14px' }}
              >
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: '14px', fontWeight: 500 }}>
                    {entry.name}
                    {entry.version ? <span style={{ fontSize: '11px', color: 'var(--text-muted)', marginLeft: '8px' }}>v{entry.version}</span> : null}
                  </div>
                  {entry.description && (
                    <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>{entry.description}</div>
                  )}
                </div>
                <button
                  onClick={() => prepareInstall(entry)}
                  style={{ background: 'var(--bg-hover)', border: '1px solid var(--border-strong)', padding: '6px 14px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px', color: 'var(--text-primary)' }}
                >
                  Install…
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {installError && (
        <p data-testid="skill-install-error" style={{ color: 'var(--danger)', fontSize: '13px', margin: '0 0 16px 0' }}>
          {installError}
        </p>
      )}
      {installNotice && (
        <p data-testid="skill-install-notice" style={{ color: 'var(--accent)', fontSize: '13px', margin: '0 0 16px 0' }}>
          {installNotice}
        </p>
      )}
      {pendingPin && (
        <SkillInstallCard
          pin={pendingPin}
          onConfirm={confirmInstall}
          onCancel={() => setPendingPin(null)}
        />
      )}

      {skills.length > 0 && (
        <CapabilityFilterBar
          query={capQuery}
          onQueryChange={setCapQuery}
          filter={capFilter}
          onFilterChange={setCapFilter}
          counts={counts}
          resultCount={visibleEntries.length}
          totalCount={entries.length}
          searchPlaceholder="Search skills by name…"
          testId="skills-filter-bar"
        />
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
        {!skillsAnswered && skills.length === 0 ? (
          <LoadStateRow
            loadState="loading"
            isConnected={isConnected}
            testId="skills-load-state"
            loadingText="Loading skills…"
            offlineText="Waiting for the engine — skills from your workspace will appear here once it connects."
          />
        ) : skills.length === 0 ? (
          <div style={{ color: 'var(--text-secondary)', fontSize: '14px', padding: '24px', textAlign: 'center', background: 'var(--bg-base)', borderRadius: '12px' }}>
            No skills found in workspace. Create a skill in <code>.agents/skills/</code>.
          </div>
        ) : visibleEntries.length === 0 ? (
          <EmptyState
            variant="no-results"
            icon={<Search size={26} color="var(--text-muted)" />}
            title="No matching skills"
            description="Nothing matches the current search and filters."
            actionLabel="Show all skills"
            onAction={clearCapabilityFilter}
            testId="skills-no-results"
          />
        ) : (
          visibleEntries.map(({ item: skill }) => skill.state === 'load_error' ? (
            // D4-5 (FF4-07): a skill whose manifest failed to
            // parse. The engine lists it as a load_error row
            // (never dropped), so the panel renders exactly that:
            // the directory name, an Invalid marker and the parse
            // reason. There is nothing to toggle — it never
            // loaded, so it is not available to agents.
            <CapabilityItem
              key={skill.name}
              name={skill.name}
              tone="error"
              badges={<span style={{ fontSize: '11px', background: 'rgba(201, 42, 42, 0.15)', color: 'var(--danger)', padding: '2px 6px', borderRadius: '4px' }}>Invalid</span>}
            >
              <p style={{ fontSize: '13px', color: 'var(--danger)', marginTop: '4px' }}>
                {skill.error}
              </p>
              <p style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '4px' }}>
                This skill could not be loaded, so it is not available to agents.
              </p>
            </CapabilityItem>
          ) : (
            <CapabilityItem
              key={skill.name}
              name={skill.name}
              badges={(
                <>
                  <span style={{ fontSize: '11px', background: 'var(--bg-hover)', padding: '2px 6px', borderRadius: '4px' }}>{skill.source}</span>
                  {skill.pin?.status === 'drifted' && (
                    <span style={{ fontSize: '11px', background: 'rgba(var(--accent-rgb), 0.15)', color: 'var(--accent)', padding: '2px 6px', borderRadius: '4px' }}>Modified vs pin</span>
                  )}
                </>
              )}
              description={skill.description}
            >
              {skill.pin && (
                <p style={{ fontSize: '12px', color: 'var(--text-muted)', marginTop: '8px' }}>
                  Installed from {skill.pin.source} · pin {String(skill.pin.hash || '').slice(0, 12)}…
                  {skill.pin.status === 'drifted'
                    ? ' The files on disk no longer match the recorded pin.'
                    : ' The files on disk match the recorded pin.'}
                </p>
              )}
              {((skill.slash_commands?.length > 0) || skill.required_tools?.length > 0) && (
                <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginTop: '12px' }}>
                  {skill.slash_commands?.map(cmd => (
                    <span key={cmd} style={{ fontSize: '12px', background: 'rgba(var(--accent-rgb), 0.15)', color: 'var(--accent)', padding: '4px 8px', borderRadius: '6px' }}>
                      {cmd}
                    </span>
                  ))}
                  {skill.required_tools?.length > 0 && (
                    <span style={{ fontSize: '12px', background: 'rgba(var(--accent-rgb), 0.15)', color: 'var(--accent)', padding: '4px 8px', borderRadius: '6px' }}>
                      {skill.required_tools.length} Tools Required
                    </span>
                  )}
                </div>
              )}
              <div style={{ marginTop: '12px', display: 'flex', alignItems: 'center', gap: '10px' }}>
                <button
                  onClick={() => onSkillToggle(skill)}
                  disabled={Boolean(pendingSkillToggleFor(skill.name))}
                  style={{ background: skill.enabled === false ? 'rgba(76, 195, 138,0.15)' : 'rgba(201,42,42,0.12)', color: skill.enabled === false ? 'var(--success)' : 'var(--danger)', border: '1px solid var(--border-default)', padding: '6px 14px', borderRadius: '8px', cursor: pendingSkillToggleFor(skill.name) ? 'default' : 'pointer', fontSize: '12px', opacity: pendingSkillToggleFor(skill.name) ? 0.55 : 1 }}
                >
                  {skill.enabled === false ? 'Enable skill' : 'Disable skill'}
                </button>
                {pendingSkillToggleFor(skill.name) && (
                  <span style={{ fontSize: '12px', color: 'var(--accent)' }}>
                    {pendingSkillToggleFor(skill.name).payload.enabled ? 'Enable' : 'Disable'} queued — will apply when the engine reconnects
                  </span>
                )}
                {skill.enabled === false && (
                  <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>Disabled — hidden from agents</span>
                )}
              </div>
            </CapabilityItem>
          ))
        )}
      </div>
    </div>
  );
}
