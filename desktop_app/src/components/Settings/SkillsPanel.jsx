import React from 'react';
import LoadStateRow from './LoadStateRow';

// Wave 5 (Annex C row 7): the Skills tab, extracted from
// SettingsPanel.jsx — the shell keeps the skill list state and
// the toggle send; this panel is the controlled rendering.
export default function SkillsPanel({ skills, skillsAnswered, isConnected, pendingSkillToggleFor, onSkillToggle }) {
  return (
    <div style={{ color: 'var(--text-primary)' }}>
      <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Skills</h2>
      <p style={{ color: 'var(--text-muted)', fontSize: '14px', marginBottom: '24px' }}>
        Dynamic behaviors, prompts, and tool requirements loaded from `.agents/skills`. Trigger these in chat with slash commands.
      </p>
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
        ) : (
          skills.map(skill => skill.state === 'load_error' ? (
            // D4-5 (FF4-07): a skill whose manifest failed to
            // parse. The engine lists it as a load_error row
            // (never dropped), so the panel renders exactly that:
            // the directory name, an Invalid marker and the parse
            // reason. There is nothing to toggle — it never
            // loaded, so it is not available to agents.
            <div key={skill.name} style={{ background: 'var(--bg-raised)', border: '1px solid rgba(201, 42, 42, 0.3)', borderRadius: '12px', padding: '16px' }}>
              <h4 style={{ fontSize: '15px', fontWeight: 500, display: 'flex', alignItems: 'center', gap: '8px' }}>
                {skill.name}
                <span style={{ fontSize: '11px', background: 'rgba(201, 42, 42, 0.15)', color: 'var(--danger)', padding: '2px 6px', borderRadius: '4px' }}>Invalid</span>
              </h4>
              <p style={{ fontSize: '13px', color: 'var(--danger)', marginTop: '4px' }}>
                {skill.error}
              </p>
              <p style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '4px' }}>
                This skill could not be loaded, so it is not available to agents.
              </p>
            </div>
          ) : (
            <div key={skill.name} style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: '12px', padding: '16px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '12px' }}>
                <div>
                  <h4 style={{ fontSize: '15px', fontWeight: 500, display: 'flex', alignItems: 'center', gap: '8px' }}>
                    {skill.name}
                    <span style={{ fontSize: '11px', background: 'var(--bg-hover)', padding: '2px 6px', borderRadius: '4px' }}>{skill.source}</span>
                  </h4>
                  <p style={{ fontSize: '13px', color: 'var(--text-muted)', marginTop: '4px' }}>
                    {skill.description}
                  </p>
                </div>
              </div>
              <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
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
            </div>
          ))
        )}
      </div>
    </div>
  );
}
