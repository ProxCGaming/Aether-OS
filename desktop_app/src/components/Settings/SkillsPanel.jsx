import React from 'react';
import LoadStateRow from './LoadStateRow';

// Wave 5 (Annex C row 7): the Skills tab, extracted from
// SettingsPanel.jsx — the shell keeps the skill list state and
// the toggle send; this panel is the controlled rendering.
export default function SkillsPanel({ skills, skillsAnswered, isConnected, pendingSkillToggleFor, onSkillToggle }) {
  return (
    <div style={{ color: '#f0f0f5' }}>
      <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Skills</h2>
      <p style={{ color: '#9090a0', fontSize: '14px', marginBottom: '24px' }}>
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
          <div style={{ color: '#9090a0', fontSize: '14px', padding: '24px', textAlign: 'center', background: 'rgba(0,0,0,0.2)', borderRadius: '12px' }}>
            No skills found in workspace. Create a skill in <code>.agents/skills/</code>.
          </div>
        ) : (
          skills.map(skill => (
            <div key={skill.name} style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '12px', padding: '16px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '12px' }}>
                <div>
                  <h4 style={{ fontSize: '15px', fontWeight: 500, display: 'flex', alignItems: 'center', gap: '8px' }}>
                    {skill.name}
                    <span style={{ fontSize: '11px', background: 'rgba(255,255,255,0.1)', padding: '2px 6px', borderRadius: '4px' }}>{skill.source}</span>
                  </h4>
                  <p style={{ fontSize: '13px', color: '#9090a0', marginTop: '4px' }}>
                    {skill.description}
                  </p>
                </div>
              </div>
              <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                {skill.slash_commands?.map(cmd => (
                  <span key={cmd} style={{ fontSize: '12px', background: 'rgba(59, 130, 246, 0.2)', color: '#60a5fa', padding: '4px 8px', borderRadius: '6px' }}>
                    {cmd}
                  </span>
                ))}
                {skill.required_tools?.length > 0 && (
                  <span style={{ fontSize: '12px', background: 'rgba(251, 191, 36, 0.15)', color: '#fbbf24', padding: '4px 8px', borderRadius: '6px' }}>
                    {skill.required_tools.length} Tools Required
                  </span>
                )}
              </div>
              <div style={{ marginTop: '12px', display: 'flex', alignItems: 'center', gap: '10px' }}>
                <button
                  onClick={() => onSkillToggle(skill)}
                  disabled={Boolean(pendingSkillToggleFor(skill.name))}
                  style={{ background: skill.enabled === false ? 'rgba(16,185,129,0.15)' : 'rgba(239,68,68,0.12)', color: skill.enabled === false ? '#10b981' : '#f87171', border: '1px solid rgba(255,255,255,0.12)', padding: '6px 14px', borderRadius: '8px', cursor: pendingSkillToggleFor(skill.name) ? 'default' : 'pointer', fontSize: '12px', opacity: pendingSkillToggleFor(skill.name) ? 0.55 : 1 }}
                >
                  {skill.enabled === false ? 'Enable skill' : 'Disable skill'}
                </button>
                {pendingSkillToggleFor(skill.name) && (
                  <span style={{ fontSize: '12px', color: '#fbbf24' }}>
                    {pendingSkillToggleFor(skill.name).payload.enabled ? 'Enable' : 'Disable'} queued — will apply when the engine reconnects
                  </span>
                )}
                {skill.enabled === false && (
                  <span style={{ fontSize: '12px', color: '#9090a0' }}>Disabled — hidden from agents</span>
                )}
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
