import React, { useState, useEffect } from 'react';
import { Trash2 } from 'lucide-react';

// E6-F05 (Epic 2 handoff): editing UI for the per-path policy rules
// (UserConfig.path_rules) that refine tool policies for calls under
// a path prefix. Until now they were config-file only.
//
// The panel is controlled: SettingsPanel owns the socket and applies
// config payloads through its single applyConfigValues point; this
// component receives the stored rules, keeps the row edits locally,
// and reports the cleaned list via onConfigUpdate. Rules are
// ordered — the first match wins — and each rule carries exactly the
// shape tools/policy.py consumes: {prefix, verdict, tools?, risks?}.
// An empty scope list is omitted from the stored rule rather than
// stored empty (the consumer reads an empty list as "unscoped"
// either way, but the file should say what the editor shows).

const VERDICTS = ['Always Allow', 'Require Approval', 'Deny'];
const RISK_VALUES = ['read_only', 'write', 'execute', 'destructive'];

const inputStyle = {
  background: 'rgba(0,0,0,0.2)',
  border: '1px solid rgba(255,255,255,0.1)',
  borderRadius: '6px',
  padding: '9px 12px',
  color: '#fff',
  fontSize: '13px',
};

const toRow = (rule) => ({
  prefix: rule?.prefix || '',
  verdict: VERDICTS.includes(rule?.verdict) ? rule.verdict : 'Require Approval',
  tools: (rule?.tools || []).join(', '),
  risks: (rule?.risks || []).join(', '),
});

const splitList = (text) => text.split(',').map((s) => s.trim()).filter(Boolean);

export default function PathRulesPanel({ rules, configError, onConfigUpdate }) {
  const [rows, setRows] = useState(() => (rules || []).map(toRow));
  const [localError, setLocalError] = useState('');
  const [saved, setSaved] = useState(false);

  // Re-sync from the applied config (initial CONFIG_GET, and the
  // echo of this panel's own save once the engine has stored it).
  useEffect(() => {
    setRows((rules || []).map(toRow));
  }, [rules]);

  const error = localError || configError || '';

  const updateRow = (index, patch) => {
    setRows((prev) => prev.map((row, i) => (i === index ? { ...row, ...patch } : row)));
    setSaved(false);
  };

  const handleSave = () => {
    const cleaned = [];
    for (let i = 0; i < rows.length; i += 1) {
      const row = rows[i];
      const prefix = row.prefix.trim();
      if (!prefix) {
        setLocalError(`Rule ${i + 1} needs a path prefix.`);
        return;
      }
      const risks = splitList(row.risks);
      const badRisk = risks.find((r) => !RISK_VALUES.includes(r));
      if (badRisk) {
        setLocalError(`Rule ${i + 1}: unknown risk "${badRisk}" (use ${RISK_VALUES.join(', ')}).`);
        return;
      }
      const rule = { prefix, verdict: row.verdict };
      const tools = splitList(row.tools);
      if (tools.length) rule.tools = tools;
      if (risks.length) rule.risks = risks;
      cleaned.push(rule);
    }
    setLocalError('');
    setSaved(true);
    onConfigUpdate({ path_rules: cleaned });
  };

  return (
    <div data-testid="path-rules-panel" style={{ marginBottom: '24px' }}>
      <h3 style={{ fontSize: '16px', fontWeight: 500, marginBottom: '4px' }}>Path rules</h3>
      <p style={{ fontSize: '13px', color: '#9090a0', marginBottom: '12px' }}>
        Per-path refinements to the tool policies above, for calls whose arguments point
        under a prefix. Rules are checked in order — the first match wins — and a rule can
        never override an explicit per-tool Deny or Require Approval, or the sensitive-path floor.
      </p>
      {error && (
        <div
          data-testid="path-rules-error"
          style={{ background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.3)', borderRadius: '10px', padding: '10px 14px', fontSize: '13px', color: '#fca5a5', marginBottom: '12px' }}
        >
          {error}
        </div>
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '10px', marginBottom: '12px' }}>
        {rows.length === 0 ? (
          <div style={{ color: '#9090a0', fontSize: '14px', padding: '16px', textAlign: 'center', background: 'rgba(0,0,0,0.2)', borderRadius: '12px' }}>
            No path rules. Tool policies apply as-is.
          </div>
        ) : (
          rows.map((row, i) => (
            <div
              key={i}
              style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '12px', padding: '14px 16px' }}
            >
              <div style={{ display: 'flex', gap: '8px', alignItems: 'center', marginBottom: '8px' }}>
                <input
                  data-testid={`path-rule-prefix-${i}`}
                  value={row.prefix}
                  onChange={(e) => updateRow(i, { prefix: e.target.value })}
                  placeholder="/path/prefix (e.g. ~/projects)"
                  aria-label={`Rule ${i + 1} path prefix`}
                  style={{ ...inputStyle, flex: 1, fontFamily: 'monospace' }}
                />
                <select
                  data-testid={`path-rule-verdict-${i}`}
                  value={row.verdict}
                  onChange={(e) => updateRow(i, { verdict: e.target.value })}
                  aria-label={`Rule ${i + 1} verdict`}
                  style={{ ...inputStyle, width: '190px' }}
                >
                  {VERDICTS.map((v) => (
                    <option key={v} value={v} style={{ background: '#1a1a22' }}>{v}</option>
                  ))}
                </select>
                <button
                  type="button"
                  data-testid={`path-rule-remove-${i}`}
                  onClick={() => {
                    setRows((prev) => prev.filter((_, idx) => idx !== i));
                    setSaved(false);
                  }}
                  aria-label={`Remove rule ${i + 1}`}
                  style={{ background: 'rgba(239,68,68,0.12)', color: '#f87171', border: '1px solid rgba(239,68,68,0.3)', borderRadius: '8px', padding: '8px 10px', cursor: 'pointer', display: 'flex', alignItems: 'center' }}
                >
                  <Trash2 size={14} />
                </button>
              </div>
              <div style={{ display: 'flex', gap: '8px' }}>
                <input
                  data-testid={`path-rule-tools-${i}`}
                  value={row.tools}
                  onChange={(e) => updateRow(i, { tools: e.target.value })}
                  placeholder="Only these tools (comma-separated, optional)"
                  aria-label={`Rule ${i + 1} tools`}
                  style={{ ...inputStyle, flex: 1 }}
                />
                <input
                  data-testid={`path-rule-risks-${i}`}
                  value={row.risks}
                  onChange={(e) => updateRow(i, { risks: e.target.value })}
                  placeholder={`Only these risks: ${RISK_VALUES.join(', ')}`}
                  aria-label={`Rule ${i + 1} risks`}
                  style={{ ...inputStyle, flex: 1 }}
                />
              </div>
            </div>
          ))
        )}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
        <button
          type="button"
          data-testid="path-rule-add"
          onClick={() => {
            setRows((prev) => [...prev, toRow(null)]);
            setSaved(false);
          }}
          style={{ background: 'rgba(255,255,255,0.06)', color: '#f0f0f5', border: '1px solid rgba(255,255,255,0.15)', padding: '8px 14px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
        >
          Add rule
        </button>
        <button
          type="button"
          data-testid="path-rules-save"
          onClick={handleSave}
          style={{ background: 'rgba(124,58,237,0.15)', color: '#c4b5fd', border: '1px solid rgba(124,58,237,0.4)', padding: '8px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px', fontWeight: 500 }}
        >
          Save path rules
        </button>
        {saved && !error && <span style={{ fontSize: '13px', color: '#10b981' }}>Saved.</span>}
      </div>
    </div>
  );
}
