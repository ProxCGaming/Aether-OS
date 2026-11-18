import React, { useState } from 'react';
import { Search } from 'lucide-react';
import LoadStateRow from './LoadStateRow';
import EmptyState from '../EmptyState';
import CapabilityFilterBar from './CapabilityFilterBar';
import CapabilityItem from './CapabilityItem';
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
  const [capQuery, setCapQuery] = useState('');
  const [capFilter, setCapFilter] = useState('all');
  const entries = skills.map((skill) => ({
    item: skill,
    name: skill.name,
    state: skill.state === 'load_error' ? 'error' : (skill.enabled === false ? 'disabled' : 'enabled'),
    hasUpdate: false,
  }));
  const counts = capabilityFilterCounts(entries);
  const visibleEntries = filterCapabilityItems(entries, capQuery, capFilter);
  const clearCapabilityFilter = () => { setCapQuery(''); setCapFilter('all'); };
  return (
    <div style={{ color: 'var(--text-primary)' }}>
      <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Skills</h2>
      <p style={{ color: 'var(--text-muted)', fontSize: '14px', marginBottom: '24px' }}>
        Dynamic behaviors, prompts, and tool requirements loaded from `.agents/skills`. Trigger these in chat with slash commands.
      </p>
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
              badges={<span style={{ fontSize: '11px', background: 'var(--bg-hover)', padding: '2px 6px', borderRadius: '4px' }}>{skill.source}</span>}
              description={skill.description}
            >
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
