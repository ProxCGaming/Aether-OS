import React, { useState, useEffect } from 'react';

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

const SPECIALISTS = ['researcher', 'planner', 'coder'];

const cardStyle = {
  background: 'rgba(255,255,255,0.02)',
  border: '1px solid rgba(255,255,255,0.05)',
  borderRadius: '12px',
  padding: '24px',
};

const inputStyle = {
  background: 'rgba(0,0,0,0.2)',
  border: '1px solid rgba(255,255,255,0.1)',
  borderRadius: '6px',
  padding: '10px 12px',
  color: '#fff',
  fontSize: '13px',
};

const saveButtonStyle = {
  background: 'rgba(124,58,237,0.15)',
  color: '#c4b5fd',
  border: '1px solid rgba(124,58,237,0.4)',
  padding: '8px 16px',
  borderRadius: '8px',
  cursor: 'pointer',
  fontSize: '13px',
  fontWeight: 500,
};

const canonical = (names) => SPECIALISTS.filter((s) => names.includes(s));

export default function OrchestrationPanel({ config, configError, onConfigUpdate, configReady }) {
  // RUX-C-11: until the first config answer lands, every control
  // below holds a local default — presenting those as the user's
  // saved configuration was the lie. The whole control set is
  // disabled (one fieldset) until the config is actually known;
  // SettingsPanel's banner above the panel explains why. Callers
  // that don't pass configReady get the derived rule: a non-null
  // config IS an answer.
  const ready = configReady !== undefined ? configReady : config != null;
  const [spawnRestricted, setSpawnRestricted] = useState(false);
  const [spawnAllowed, setSpawnAllowed] = useState([...SPECIALISTS]);
  const [delegationCap, setDelegationCap] = useState('8');
  const [capDirty, setCapDirty] = useState(false);
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
      setSpawnAllowed([...SPECIALISTS]);
    } else {
      setSpawnRestricted(true);
      setSpawnAllowed(allow);
    }
    if (!capDirty && config.supervisor_delegation_cap !== undefined && config.supervisor_delegation_cap !== null) {
      setDelegationCap(String(config.supervisor_delegation_cap));
    }
    setPrewalk(config.prewalk_agents || []);
    const sch = config.agent_output_schemas || {};
    setSchemas(sch);
    setSchemaText((prev) => {
      const next = { ...prev };
      for (const a of SPECIALISTS) {
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

  const error = localError || configError || '';

  const handleRestrictToggle = (checked) => {
    setSpawnRestricted(checked);
    setLocalError('');
    onConfigUpdate({
      supervisor_spawn_allowlist: checked ? canonical(spawnAllowed) : null,
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

  const checkboxRow = (testId, name, checked, onChange, disabled = false) => (
    <label
      key={name}
      style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '14px', color: disabled ? '#6b6b7a' : '#f0f0f5', cursor: disabled ? 'default' : 'pointer' }}
    >
      <input
        type="checkbox"
        data-testid={testId}
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(name, e.target.checked)}
        style={{ accentColor: '#7c3aed', width: '16px', height: '16px' }}
      />
      {name}
    </label>
  );

  return (
    <div style={{ color: '#f0f0f5', display: 'flex', flexDirection: 'column', gap: '20px' }}>
      <div>
        <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Agent Orchestration</h2>
        <p style={{ color: '#9090a0', fontSize: '14px', margin: 0 }}>
          How the supervisor delegates, when specialists switch to their execution model,
          what shape their answers must take, and when long conversations are compacted.
        </p>
      </div>

      {error && (
        <div
          data-testid="orchestration-error"
          style={{ background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.3)', borderRadius: '10px', padding: '12px 16px', fontSize: '13px', color: '#fca5a5' }}
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
        <h3 style={{ fontSize: '16px', fontWeight: 500, marginBottom: '8px', color: '#fff' }}>Supervisor spawn policy</h3>
        <p style={{ fontSize: '13px', color: '#9090a0', margin: '0 0 16px 0' }}>
          Restrict which specialists the supervisor may start, and how many delegations
          it may make per user turn. A blocked delegation is explained in the chat, never silent.
        </p>
        <label style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '14px', marginBottom: '16px', cursor: 'pointer' }}>
          <input
            type="checkbox"
            data-testid="spawn-restrict-toggle"
            checked={spawnRestricted}
            onChange={(e) => handleRestrictToggle(e.target.checked)}
            style={{ accentColor: '#7c3aed', width: '16px', height: '16px' }}
          />
          Restrict which specialists may be started
        </label>
        <div style={{ display: 'flex', gap: '20px', marginBottom: '20px' }}>
          {SPECIALISTS.map((name) => checkboxRow(
            `spawn-allow-${name}`,
            name,
            spawnRestricted ? spawnAllowed.includes(name) : true,
            handleSpawnToggle,
            !spawnRestricted
          ))}
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
          <span style={{ fontSize: '12px', color: '#9090a0' }}>0 blocks all delegation. Applies when you leave the field.</span>
        </div>
      </div>

      <div style={cardStyle}>
        <h3 style={{ fontSize: '16px', fontWeight: 500, marginBottom: '8px', color: '#fff' }}>Prewalk</h3>
        <p style={{ fontSize: '13px', color: '#9090a0', margin: '0 0 16px 0' }}>
          A prewalk specialist plans on the run&apos;s model and switches to its own
          Agents-tab model only when it first writes or executes something.
        </p>
        <div style={{ display: 'flex', gap: '20px' }}>
          {SPECIALISTS.map((name) => checkboxRow(
            `prewalk-${name}`, name, prewalk.includes(name), handlePrewalkToggle))}
        </div>
      </div>

      <div style={cardStyle}>
        <h3 style={{ fontSize: '16px', fontWeight: 500, marginBottom: '8px', color: '#fff' }}>Agent output schemas</h3>
        <p style={{ fontSize: '13px', color: '#9090a0', margin: '0 0 16px 0' }}>
          A JSON Schema for a specialist&apos;s final answer. The answer is validated
          before it is accepted; a mismatch gets one correction retry, then fails loudly.
          Leave empty for free-form answers.
        </p>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
          {SPECIALISTS.map((name) => (
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
                style={{ ...inputStyle, width: '100%', fontFamily: 'monospace', resize: 'vertical', boxSizing: 'border-box' }}
              />
            </div>
          ))}
        </div>
      </div>

      <div style={cardStyle}>
        <h3 style={{ fontSize: '16px', fontWeight: 500, marginBottom: '8px', color: '#fff' }}>Compaction thresholds</h3>
        <p style={{ fontSize: '13px', color: '#9090a0', margin: '0 0 16px 0' }}>
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
