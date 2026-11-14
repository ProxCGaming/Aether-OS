import React, { useState, useEffect, useMemo } from 'react';
import { useAetherSocket } from '../../contexts/SocketContext';

// E6-F05 (Epic 1 handoff): Settings surfaces for the orchestration
// config the engine has honoured since Epic 1 but nobody could edit
// without hand-editing the config file — supervisor spawn policy,
// prewalk agents, per-agent output schemas and compaction thresholds.
//
// The panel is controlled: SettingsPanel owns the socket and applies
// every CONFIG_GET / CONFIG_UPDATE / CONFIG_RESET payload through
// its single applyConfigValues point; this component receives the
// applied config, renders it, and reports edits via onConfigUpdate.
// The engine validates these fields strictly and refuses malformed
// writes with an error reply, surfaced here verbatim (configError).
//
// Free-text edits (cap, compaction numbers, schema JSON) are held
// locally and marked dirty so a config echo — e.g. from toggling a
// checkbox — never clobbers text the user is still typing; the echo
// of the field's own save re-syncs it.

const cardStyle = {
  background: 'var(--bg-raised)',
  border: '1px solid var(--border-default)',
  borderRadius: '12px',
  padding: '24px',
};

const inputStyle = {
  background: 'var(--bg-base)',
  border: '1px solid var(--border-default)',
  borderRadius: 'var(--radius-md)',
  padding: '10px 12px',
  color: 'var(--text-primary)',
  fontSize: '13px',
};

const saveButtonStyle = {
  background: 'rgba(var(--accent-rgb),0.15)',
  color: 'var(--accent)',
  border: '1px solid rgba(var(--accent-rgb),0.4)',
  padding: '8px 16px',
  borderRadius: '8px',
  cursor: 'pointer',
  fontSize: '13px',
  fontWeight: 500,
};

// S21 (D2-38): the panel's agent roster comes from the engine's
// AGENT_LIST answer — the same report the Agents tab renders —
// not the hard-coded trio this file used to carry. AGENT_LIST
// reports every agent including the supervisor, each with its
// Agents-tab model pin; the supervisor is the delegator, never
// a delegation target, so the spawn / prewalk / schema cards
// enumerate the specialists (the list minus the supervisor).
// S14's AGENT_ROUTING report stays the source for EFFECTIVE
// models (pin vs auto vs run default, with reasons); this list
// is the source for who exists and what they are pinned to.
function useAgentList() {
  const { wsRef, sendMessage, reconnectCount = 0 } = useAetherSocket();
  const [agents, setAgents] = useState(null);
  const [agentsError, setAgentsError] = useState('');

  useEffect(() => {
    sendMessage('AGENT_LIST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });

    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'AGENT_LIST_RESPONSE') {
          // RUX-C-10 pattern (AgentsPanel): an errored reply is
          // the error state, never an empty roster.
          if (data.payload && data.payload.error) {
            setAgentsError(data.payload.error);
          } else {
            setAgents((data.payload && data.payload.agents) || []);
            setAgentsError('');
          }
        }
      } catch (e) {
        // Not JSON / not ours — other panels own their replies.
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
  }, [wsRef, sendMessage, reconnectCount]);

  return { agents, agentsError };
}

const ROUTING_SOURCE_LABELS = {
  pinned: 'Pinned by you',
  auto: 'Auto routing',
  run_default: 'Run default',
};

// S14: the read-only effective-model list inside the Model routing
// card. Self-contained like the S10 machine card: it asks the
// engine for the per-agent routing report on mount, on reconnect
// and whenever the mode changes, and renders the engine's verdicts
// and reasons verbatim — it computes nothing itself. The report's
// per-agent decisions are computed on NEUTRAL signals (no task),
// so the card labels them as the default decisions: each real
// delegation is decided per task and can differ.
function EffectiveAgentModels({ mode }) {
  const { wsRef, sendMessage, isConnected, reconnectCount = 0 } = useAetherSocket();
  const [report, setReport] = useState(null);

  useEffect(() => {
    sendMessage('AGENT_ROUTING_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });

    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'AGENT_ROUTING_RESPONSE') {
          setReport(data.payload);
        }
      } catch (e) {
        // Not JSON / not ours — other panels own their replies.
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
  }, [wsRef, sendMessage, reconnectCount, mode]);

  if (!report) {
    return (
      <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
        {isConnected ? 'Asking the engine which models would serve each agent…' : 'Waiting for the engine — the effective models will appear here once it connects.'}
      </div>
    );
  }
  return (
    <div data-testid="model-routing-effective-list" style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
      {(report.agents || []).map((row) => (
        <div
          key={row.agent}
          data-testid={`model-routing-agent-${row.agent}`}
          style={{ background: 'var(--bg-base)', border: '1px solid var(--border-default)', borderRadius: '10px', padding: '10px 14px' }}
        >
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '12px' }}>
            <span style={{ fontSize: '14px', fontWeight: 500 }}>{row.agent}</span>
            <span style={{ fontSize: '13px', color: 'var(--text-secondary)', fontFamily: 'var(--font-mono)' }}>
              {row.provider ? `${row.provider}:` : ''}{row.model}
            </span>
            <span style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', border: '1px solid var(--border-default)', borderRadius: '999px', padding: '2px 10px', whiteSpace: 'nowrap' }}>
              {ROUTING_SOURCE_LABELS[row.source] || row.source}
            </span>
          </div>
          <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '4px' }}>
            {row.reason}
          </div>
        </div>
      ))}
      <p style={{ fontSize: '12px', color: 'var(--text-muted)', margin: '4px 0 0 0' }}>
        Default decisions on a neutral task. Each real delegation is decided per task —
        its length, its kind and the models&apos; recorded track record can change the pick,
        and a failed run steps up one tier, once.
      </p>
    </div>
  );
}

export default function OrchestrationPanel({ config, configError, onConfigUpdate, configReady }) {
  // RUX-C-11: until the first config answer lands, every control
  // below holds a local default — presenting those as the user's
  // saved configuration was the lie. The whole control set is
  // disabled (one fieldset) until the config is actually known;
  // SettingsPanel's banner above the panel explains why. Callers
  // that don't pass configReady get the derived rule: a non-null
  // config IS an answer.
  const ready = configReady !== undefined ? configReady : config != null;
  // S21 (D2-38): the roster the cards enumerate — the engine's
  // AGENT_LIST specialists, in engine order. Null until the
  // answer lands: before that, the enumerating cards show a
  // load note instead of a guessed set of agents.
  const { agents, agentsError } = useAgentList();
  const { isConnected } = useAetherSocket();
  // Memoised on the answer: effects below key off this list's
  // identity, so it must change only when a new answer lands.
  const specialists = useMemo(
    () => (agents ? agents.filter((a) => a.id !== 'supervisor').map((a) => a.id) : null),
    [agents]
  );
  const canonical = (names) => {
    if (!specialists) return [...names];
    const ordered = specialists.filter((s) => names.includes(s));
    // A name the engine no longer reports (a stale config entry)
    // keeps its place at the end rather than vanishing mid-edit.
    return [...ordered, ...names.filter((n) => !specialists.includes(n))];
  };
  const [spawnRestricted, setSpawnRestricted] = useState(false);
  const [spawnAllowed, setSpawnAllowed] = useState([]);
  const [delegationCap, setDelegationCap] = useState('8');
  const [capDirty, setCapDirty] = useState(false);
  const [subagentsEnabled, setSubagentsEnabled] = useState(true);
  const [modelRouting, setModelRouting] = useState('off');
  const [subagentBounds, setSubagentBounds] = useState({
    maxPerTurn: '4', timeoutS: '120', tokenBudget: '20000', costBudget: '0',
  });
  const [subagentBoundsDirty, setSubagentBoundsDirty] = useState(false);
  const [prewalk, setPrewalk] = useState([]);
  const [schemas, setSchemas] = useState({});
  const [schemaText, setSchemaText] = useState({});
  const [dirtySchemas, setDirtySchemas] = useState({});
  const [compaction, setCompaction] = useState({
    messages: '40', chars: '24000', keep: '12',
  });
  const [compactionDirty, setCompactionDirty] = useState(false);
  const [localError, setLocalError] = useState('');

  useEffect(() => {
    if (!config) return;
    const allow = config.supervisor_spawn_allowlist;
    if (allow === null || allow === undefined) {
      setSpawnRestricted(false);
      // S21: an unrestricted config seeds the allowed set from
      // the engine roster when it is known (the roster effect
      // below seeds it when the list lands after the config).
      setSpawnAllowed(specialists ? [...specialists] : []);
    } else {
      setSpawnRestricted(true);
      setSpawnAllowed(allow);
    }
    if (!capDirty && config.supervisor_delegation_cap !== undefined && config.supervisor_delegation_cap !== null) {
      setDelegationCap(String(config.supervisor_delegation_cap));
    }
    setSubagentsEnabled(config.subagents_enabled !== false);
    setModelRouting(config.model_routing === 'auto' ? 'auto' : 'off');
    if (!subagentBoundsDirty) {
      setSubagentBounds({
        maxPerTurn: String(config.subagents_max_per_turn ?? 4),
        timeoutS: String(config.subagent_timeout_s ?? 120),
        tokenBudget: String(config.subagent_token_budget ?? 20000),
        costBudget: String(config.subagent_cost_budget_usd ?? 0),
      });
    }
    setPrewalk(config.prewalk_agents || []);
    const sch = config.agent_output_schemas || {};
    setSchemas(sch);
    setSchemaText((prev) => {
      const next = { ...prev };
      // S21: seed a textarea per engine-reported specialist,
      // plus any agent the config itself carries a schema for.
      const seedFor = [...new Set([...(specialists || []), ...Object.keys(sch)])];
      for (const a of seedFor) {
        if (!dirtySchemas[a]) {
          next[a] = sch[a] ? JSON.stringify(sch[a], null, 2) : '';
        }
      }
      return next;
    });
    if (!compactionDirty) {
      setCompaction({
        messages: String(config.compaction_trigger_messages ?? 40),
        chars: String(config.compaction_trigger_chars ?? 24000),
        keep: String(config.compaction_keep_recent ?? 12),
      });
    }
  }, [config]); // eslint-disable-line react-hooks/exhaustive-deps

  // S21: when the roster lands AFTER the config and the config
  // is unrestricted, seed the allowed set from it (the config
  // effect above can only seed from a roster already known).
  useEffect(() => {
    if (!specialists || !config) return;
    if (config.supervisor_spawn_allowlist === null
      || config.supervisor_spawn_allowlist === undefined) {
      setSpawnAllowed([...specialists]);
    }
  }, [specialists]); // eslint-disable-line react-hooks/exhaustive-deps

  const error = localError || configError || '';

  const handleRestrictToggle = (checked) => {
    setSpawnRestricted(checked);
    setLocalError('');
    // S21: turning the restriction on before the roster (and so
    // the seeded allowed set) has landed must not report an
    // empty allowlist — the roster is the seed.
    const base = spawnAllowed.length ? spawnAllowed : (specialists || []);
    if (checked && !spawnAllowed.length && specialists) {
      setSpawnAllowed([...specialists]);
    }
    onConfigUpdate({
      supervisor_spawn_allowlist: checked ? canonical(base) : null,
    });
  };

  const handleSpawnToggle = (name, checked) => {
    const next = canonical(
      checked ? [...spawnAllowed, name] : spawnAllowed.filter((s) => s !== name));
    setSpawnAllowed(next);
    setLocalError('');
    onConfigUpdate({ supervisor_spawn_allowlist: next });
  };

  const handleCapBlur = () => {
    const parsed = Number(delegationCap);
    if (!Number.isInteger(parsed) || parsed < 0 || parsed > 1000) {
      setLocalError('Delegation cap must be a whole number between 0 and 1000.');
      return;
    }
    setLocalError('');
    setCapDirty(false);
    onConfigUpdate({ supervisor_delegation_cap: parsed });
  };

  // S13: bounded sub-agent spawning — the same blur-save contract as
  // the delegation cap, one field per input, with ranges mirroring
  // the engine's CONFIG_UPDATE validators (config_updates.py).
  const handleSubagentsToggle = (checked) => {
    setSubagentsEnabled(checked);
    setLocalError('');
    onConfigUpdate({ subagents_enabled: checked });
  };

  // S14: automatic model routing — a two-value mode stored through
  // the same config path; the engine validates it strictly
  // (config_updates.py) and refuses anything else.
  const handleModelRoutingToggle = (checked) => {
    const next = checked ? 'auto' : 'off';
    setModelRouting(next);
    setLocalError('');
    onConfigUpdate({ model_routing: next });
  };

  const SUBAGENT_FIELDS = {
    maxPerTurn: ['subagents_max_per_turn', 'Sub-agents per turn', 0, 100, true],
    timeoutS: ['subagent_timeout_s', 'Sub-agent timeout (seconds)', 1, 3600, true],
    tokenBudget: ['subagent_token_budget', 'Sub-agent token budget', 0, 10000000, true],
    costBudget: ['subagent_cost_budget_usd', 'Sub-agent cost budget (USD)', 0, 10000, false],
  };

  const handleSubagentBoundBlur = (key) => {
    const [field, label, low, high, whole] = SUBAGENT_FIELDS[key];
    const parsed = Number(subagentBounds[key]);
    if (Number.isNaN(parsed) || (whole && !Number.isInteger(parsed)) || parsed < low || parsed > high) {
      setLocalError(`${label} must be ${whole ? 'a whole number' : 'a number'} between ${low} and ${high}.`);
      return;
    }
    setLocalError('');
    setSubagentBoundsDirty(false);
    onConfigUpdate({ [field]: parsed });
  };

  const handlePrewalkToggle = (name, checked) => {
    const next = canonical(
      checked ? [...prewalk, name] : prewalk.filter((s) => s !== name));
    setPrewalk(next);
    setLocalError('');
    onConfigUpdate({ prewalk_agents: next });
  };

  const handleCompactionSave = () => {
    const fields = [
      ['messages', 'Compaction trigger (messages)'],
      ['chars', 'Compaction trigger (characters)'],
      ['keep', 'Compaction keep recent'],
    ];
    const parsed = {};
    for (const [key, label] of fields) {
      const value = Number(compaction[key]);
      if (!Number.isInteger(value) || value < 1) {
        setLocalError(`${label} must be a whole number of at least 1.`);
        return;
      }
      parsed[key] = value;
    }
    setLocalError('');
    setCompactionDirty(false);
    onConfigUpdate({
      compaction_trigger_messages: parsed.messages,
      compaction_trigger_chars: parsed.chars,
      compaction_keep_recent: parsed.keep,
    });
  };

  const handleSchemaSave = (agent) => {
    const text = (schemaText[agent] || '').trim();
    const next = { ...schemas };
    if (!text) {
      delete next[agent];
    } else {
      let parsed;
      try {
        parsed = JSON.parse(text);
      } catch (e) {
        setLocalError(`Output schema for ${agent} is not valid JSON: ${e.message}`);
        return;
      }
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        setLocalError(`Output schema for ${agent} must be a JSON object.`);
        return;
      }
      next[agent] = parsed;
    }
    setSchemas(next);
    setDirtySchemas((prev) => ({ ...prev, [agent]: false }));
    setLocalError('');
    onConfigUpdate({ agent_output_schemas: next });
  };

  // S21: what the agent-enumerating cards show before the
  // engine's list lands (or when it refuses) — a stated wait,
  // never a guessed set of agents.
  const rosterNote = (
    <div data-testid="orchestration-agents-note" style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
      {agentsError
        ? `The engine could not report its agent list: ${agentsError}`
        : isConnected
          ? 'Asking the engine for its agent list…'
          : 'Waiting for the engine — the agent list will appear here once it connects.'}
    </div>
  );
  // The schema card enumerates the roster plus any agent the
  // applied config itself carries a schema for (a schema must
  // stay editable even before the list lands).
  const schemaIds = specialists
    ? canonical([...specialists, ...Object.keys(schemas)])
    : (Object.keys(schemas).length ? Object.keys(schemas) : null);

  const checkboxRow = (testId, name, checked, onChange, disabled = false) => (
    <label
      key={name}
      style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '14px', color: disabled ? 'var(--text-muted)' : 'var(--text-primary)', cursor: disabled ? 'default' : 'pointer' }}
    >
      <input
        type="checkbox"
        data-testid={testId}
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(name, e.target.checked)}
        style={{ accentColor: 'var(--accent)', width: '16px', height: '16px' }}
      />
      {name}
    </label>
  );

  return (
    <div style={{ color: 'var(--text-primary)', display: 'flex', flexDirection: 'column', gap: '20px' }}>
      <div>
        <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Agent Orchestration</h2>
        <p style={{ color: 'var(--text-muted)', fontSize: '14px', margin: 0 }}>
          How the supervisor delegates, when specialists switch to their execution model,
          what shape their answers must take, and when long conversations are compacted.
        </p>
      </div>

      {error && (
        <div
          data-testid="orchestration-error"
          style={{ background: 'rgba(201,42,42,0.08)', border: '1px solid rgba(201,42,42,0.3)', borderRadius: '10px', padding: '12px 16px', fontSize: '13px', color: 'var(--danger)' }}
        >
          {error}
        </div>
      )}

      <fieldset
        data-testid="orchestration-config-gate"
        disabled={!ready}
        style={{
          border: 0, margin: 0, padding: 0, minWidth: 0,
          display: 'flex', flexDirection: 'column', gap: '20px',
          opacity: ready ? 1 : 0.55,
        }}
      >

      <div style={cardStyle}>
        <h3 style={{ fontSize: '16px', fontWeight: 500, marginBottom: '8px', color: 'var(--text-primary)' }}>Supervisor spawn policy</h3>
        <p style={{ fontSize: '13px', color: 'var(--text-muted)', margin: '0 0 16px 0' }}>
          Restrict which specialists the supervisor may start, and how many delegations
          it may make per user turn. A blocked delegation is explained in the chat, never silent.
        </p>
        <label style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '14px', marginBottom: '16px', cursor: 'pointer' }}>
          <input
            type="checkbox"
            data-testid="spawn-restrict-toggle"
            checked={spawnRestricted}
            onChange={(e) => handleRestrictToggle(e.target.checked)}
            style={{ accentColor: 'var(--accent)', width: '16px', height: '16px' }}
          />
          Restrict which specialists may be started
        </label>
        {/* S21: the roster itself, from the engine's AGENT_LIST
            answer — every agent (supervisor included), what it
            does, and its Agents-tab pin. The checkboxes below
            enumerate this list's specialists. */}
        {agents && (
          <div data-testid="orchestration-agent-roster" style={{ display: 'flex', flexDirection: 'column', gap: '6px', marginBottom: '16px' }}>
            {agents.map((a) => (
              <div key={a.id} data-testid={`orchestration-agent-${a.id}`} style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
                <span style={{ color: 'var(--text-primary)', fontWeight: 500 }}>{a.name}</span>
                {' — '}{a.description}
                <span style={{ color: 'var(--text-muted)' }}>
                  {' · '}{a.model && a.model !== 'inherit' ? `pinned to ${a.model}` : 'no Agents-tab pin'}
                </span>
              </div>
            ))}
          </div>
        )}
        <div style={{ display: 'flex', gap: '20px', marginBottom: '20px' }}>
          {specialists ? specialists.map((name) => checkboxRow(
            `spawn-allow-${name}`,
            name,
            spawnRestricted ? spawnAllowed.includes(name) : true,
            handleSpawnToggle,
            !spawnRestricted
          )) : rosterNote}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <label htmlFor="delegation-cap-input" style={{ fontSize: '14px' }}>Delegation cap per turn</label>
          <input
            id="delegation-cap-input"
            data-testid="delegation-cap-input"
            type="number"
            min="0"
            max="1000"
            value={delegationCap}
            onChange={(e) => { setDelegationCap(e.target.value); setCapDirty(true); }}
            onBlur={handleCapBlur}
            style={{ ...inputStyle, width: '110px' }}
          />
          <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>0 blocks all delegation. Applies when you leave the field.</span>
        </div>
      </div>

      <div style={cardStyle}>
        <h3 style={{ fontSize: '16px', fontWeight: 500, marginBottom: '8px', color: 'var(--text-primary)' }}>Sub-agents</h3>
        <p style={{ fontSize: '13px', color: 'var(--text-muted)', margin: '0 0 16px 0' }}>
          A specialist may spawn a bounded sub-agent (another specialist role) for a
          focused sub-task and receives its result as a summary. Sub-agents cannot spawn
          further sub-agents, start with read-only tools, and pass the same policy and
          approval checks as their parent. These bounds are separate from the delegation
          cap above: that cap limits supervisor delegations, these limit specialist spawns.
        </p>
        <label style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '14px', marginBottom: '16px', cursor: 'pointer' }}>
          <input
            type="checkbox"
            data-testid="subagents-enabled-toggle"
            checked={subagentsEnabled}
            onChange={(e) => handleSubagentsToggle(e.target.checked)}
            style={{ accentColor: 'var(--accent)', width: '16px', height: '16px' }}
          />
          Allow specialists to spawn sub-agents
        </label>
        <div style={{ display: 'flex', gap: '16px', flexWrap: 'wrap' }}>
          <div>
            <label htmlFor="subagents-max-per-turn" style={{ display: 'block', fontSize: '13px', marginBottom: '6px' }}>Max per turn</label>
            <input
              id="subagents-max-per-turn"
              data-testid="subagents-max-per-turn"
              type="number"
              min="0"
              max="100"
              value={subagentBounds.maxPerTurn}
              onChange={(e) => { setSubagentBounds((prev) => ({ ...prev, maxPerTurn: e.target.value })); setSubagentBoundsDirty(true); }}
              onBlur={() => handleSubagentBoundBlur('maxPerTurn')}
              style={{ ...inputStyle, width: '110px' }}
            />
          </div>
          <div>
            <label htmlFor="subagent-timeout-s" style={{ display: 'block', fontSize: '13px', marginBottom: '6px' }}>Timeout (seconds)</label>
            <input
              id="subagent-timeout-s"
              data-testid="subagent-timeout-s"
              type="number"
              min="1"
              max="3600"
              value={subagentBounds.timeoutS}
              onChange={(e) => { setSubagentBounds((prev) => ({ ...prev, timeoutS: e.target.value })); setSubagentBoundsDirty(true); }}
              onBlur={() => handleSubagentBoundBlur('timeoutS')}
              style={{ ...inputStyle, width: '110px' }}
            />
          </div>
          <div>
            <label htmlFor="subagent-token-budget" style={{ display: 'block', fontSize: '13px', marginBottom: '6px' }}>Token budget per sub-agent</label>
            <input
              id="subagent-token-budget"
              data-testid="subagent-token-budget"
              type="number"
              min="0"
              max="10000000"
              value={subagentBounds.tokenBudget}
              onChange={(e) => { setSubagentBounds((prev) => ({ ...prev, tokenBudget: e.target.value })); setSubagentBoundsDirty(true); }}
              onBlur={() => handleSubagentBoundBlur('tokenBudget')}
              style={{ ...inputStyle, width: '130px' }}
            />
          </div>
          <div>
            <label htmlFor="subagent-cost-budget" style={{ display: 'block', fontSize: '13px', marginBottom: '6px' }}>Cost budget per sub-agent (USD)</label>
            <input
              id="subagent-cost-budget"
              data-testid="subagent-cost-budget"
              type="number"
              min="0"
              max="10000"
              step="0.05"
              value={subagentBounds.costBudget}
              onChange={(e) => { setSubagentBounds((prev) => ({ ...prev, costBudget: e.target.value })); setSubagentBoundsDirty(true); }}
              onBlur={() => handleSubagentBoundBlur('costBudget')}
              style={{ ...inputStyle, width: '130px' }}
            />
          </div>
        </div>
        <p style={{ fontSize: '12px', color: 'var(--text-secondary)', margin: '12px 0 0 0' }}>
          Token and cost budgets are enforced from the usage ledger&apos;s records of the
          sub-agent&apos;s own model calls; 0 disables that budget. A model provider that
          records no usage leaves the timeout and the per-turn cap as the binding bounds.
          Applies when you leave a field.
        </p>
      </div>

      <div style={cardStyle}>
        <h3 style={{ fontSize: '16px', fontWeight: 500, marginBottom: '8px', color: 'var(--text-primary)' }}>Model routing</h3>
        <p style={{ fontSize: '13px', color: 'var(--text-muted)', margin: '0 0 16px 0' }}>
          Off, every specialist without a model of its own (set in the Agents tab) runs on
          the run&apos;s model — exactly as before. On, the engine picks the model per
          delegation instead: from the sub-task itself, the models your configured
          providers offer, and each model&apos;s recorded track record. A specialist whose
          run fails steps up one model tier, once. Your Agents-tab pins always win —
          routing never overrides a pinned model, and never escalates one.
        </p>
        <label style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '14px', marginBottom: '16px', cursor: 'pointer' }}>
          <input
            type="checkbox"
            data-testid="model-routing-toggle"
            checked={modelRouting === 'auto'}
            onChange={(e) => handleModelRoutingToggle(e.target.checked)}
            style={{ accentColor: 'var(--accent)', width: '16px', height: '16px' }}
          />
          Choose models automatically per delegation
        </label>
        <EffectiveAgentModels mode={modelRouting} />
      </div>

      <div style={cardStyle}>
        <h3 style={{ fontSize: '16px', fontWeight: 500, marginBottom: '8px', color: 'var(--text-primary)' }}>Prewalk</h3>
        <p style={{ fontSize: '13px', color: 'var(--text-muted)', margin: '0 0 16px 0' }}>
          A prewalk specialist plans on the run&apos;s model and switches to its own
          Agents-tab model only when it first writes or executes something.
        </p>
        <div style={{ display: 'flex', gap: '20px' }}>
          {specialists ? specialists.map((name) => checkboxRow(
            `prewalk-${name}`, name, prewalk.includes(name), handlePrewalkToggle)) : rosterNote}
        </div>
      </div>

      <div style={cardStyle}>
        <h3 style={{ fontSize: '16px', fontWeight: 500, marginBottom: '8px', color: 'var(--text-primary)' }}>Agent output schemas</h3>
        <p style={{ fontSize: '13px', color: 'var(--text-muted)', margin: '0 0 16px 0' }}>
          A JSON Schema for a specialist&apos;s final answer. The answer is validated
          before it is accepted; a mismatch gets one correction retry, then fails loudly.
          Leave empty for free-form answers.
        </p>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
          {schemaIds ? schemaIds.map((name) => (
            <div key={name}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '6px' }}>
                <label htmlFor={`schema-${name}`} style={{ fontSize: '14px', fontWeight: 500 }}>{name}</label>
                <button
                  type="button"
                  data-testid={`schema-save-${name}`}
                  onClick={() => handleSchemaSave(name)}
                  style={saveButtonStyle}
                >
                  Save schema
                </button>
              </div>
              <textarea
                id={`schema-${name}`}
                data-testid={`schema-${name}`}
                value={schemaText[name] || ''}
                onChange={(e) => {
                  setSchemaText((prev) => ({ ...prev, [name]: e.target.value }));
                  setDirtySchemas((prev) => ({ ...prev, [name]: true }));
                }}
                placeholder='{"type": "object", "required": ["summary"]}'
                rows={4}
                spellCheck={false}
                style={{ ...inputStyle, width: '100%', fontFamily: 'var(--font-mono)', resize: 'vertical', boxSizing: 'border-box' }}
              />
            </div>
          )) : rosterNote}
        </div>
      </div>

      <div style={cardStyle}>
        <h3 style={{ fontSize: '16px', fontWeight: 500, marginBottom: '8px', color: 'var(--text-primary)' }}>Compaction thresholds</h3>
        <p style={{ fontSize: '13px', color: 'var(--text-muted)', margin: '0 0 16px 0' }}>
          When a conversation&apos;s model-facing history passes either trigger, the older
          turns fold into a summary; the most recent messages always stay verbatim.
        </p>
        <div style={{ display: 'flex', gap: '16px', flexWrap: 'wrap', marginBottom: '16px' }}>
          <div>
            <label htmlFor="compaction-trigger-messages" style={{ display: 'block', fontSize: '13px', marginBottom: '6px' }}>Trigger at messages</label>
            <input
              id="compaction-trigger-messages"
              data-testid="compaction-trigger-messages"
              type="number"
              min="1"
              value={compaction.messages}
              onChange={(e) => { setCompaction((prev) => ({ ...prev, messages: e.target.value })); setCompactionDirty(true); }}
              style={{ ...inputStyle, width: '130px' }}
            />
          </div>
          <div>
            <label htmlFor="compaction-trigger-chars" style={{ display: 'block', fontSize: '13px', marginBottom: '6px' }}>Trigger at characters</label>
            <input
              id="compaction-trigger-chars"
              data-testid="compaction-trigger-chars"
              type="number"
              min="1"
              value={compaction.chars}
              onChange={(e) => { setCompaction((prev) => ({ ...prev, chars: e.target.value })); setCompactionDirty(true); }}
              style={{ ...inputStyle, width: '130px' }}
            />
          </div>
          <div>
            <label htmlFor="compaction-keep-recent" style={{ display: 'block', fontSize: '13px', marginBottom: '6px' }}>Keep recent messages</label>
            <input
              id="compaction-keep-recent"
              data-testid="compaction-keep-recent"
              type="number"
              min="1"
              value={compaction.keep}
              onChange={(e) => { setCompaction((prev) => ({ ...prev, keep: e.target.value })); setCompactionDirty(true); }}
              style={{ ...inputStyle, width: '130px' }}
            />
          </div>
        </div>
        <button type="button" data-testid="compaction-save" onClick={handleCompactionSave} style={saveButtonStyle}>
          Save compaction thresholds
        </button>
      </div>
      </fieldset>
    </div>
  );
}
