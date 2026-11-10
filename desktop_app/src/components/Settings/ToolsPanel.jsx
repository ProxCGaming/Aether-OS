import React from 'react';
import { Lock } from 'lucide-react';
import Dropdown from '../../Dropdown';
import LoadStateRow from './LoadStateRow';
import PathRulesPanel from './PathRulesPanel';

// Wave 5 (Annex C row 7): the Tools tab, extracted from
// SettingsPanel.jsx — approval mode and binding, remembered
// grants, path rules and per-tool policies. The shell keeps the
// state and the sends; this panel is the controlled rendering.
export default function ToolsPanel({ approvalMode, handleApprovalModeChange, controlRefusal, approvalBindingMode, handleApprovalBindingChange, approvalGrants, grantsAnswered, isConnected, pendingGrantRevokeFor, handleRevokeGrant, configAnswered, engineConfig, configUpdateError, onConfigUpdate, tools, toolsAnswered, pendingToolPolicyFor, onToolPolicyChange }) {
  return (
    <div style={{ color: 'var(--text-primary)' }}>
      <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Tools Sandbox</h2>
      <p style={{ color: 'var(--text-muted)', fontSize: '14px', marginBottom: '24px' }}>Configure auto-approval policies for built-in tools.</p>

      <div style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: '12px', padding: '16px', display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
        <div>
          <h4 style={{ fontSize: '15px', fontWeight: 500 }}>Approval mode</h4>
          <p style={{ fontSize: '13px', color: 'var(--text-muted)', marginTop: '4px' }}>
            {approvalMode === 'write'
              ? 'Write tools run without asking. Execute and destructive tools still require approval.'
              : 'Every non-read-only tool asks for approval before it runs.'}
          </p>
        </div>
        <div>
          <Dropdown
            value={approvalMode}
            onChange={handleApprovalModeChange}
            options={[
              { value: 'ask', label: 'Ask for approval' },
              { value: 'write', label: 'Auto-approve writes' }
            ]}
            style={{ width: '220px' }}
            align="right"
          />
        </div>
      </div>

      {controlRefusal && (
        <div style={{ color: 'var(--danger)', fontSize: '13px', marginBottom: '12px' }}>{controlRefusal}</div>
      )}

      <div data-testid="approval-binding" style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: '12px', padding: '16px', marginBottom: '12px' }}>
        <h4 style={{ fontSize: '15px', fontWeight: 500 }}>Approval binding</h4>
        <p style={{ fontSize: '13px', color: 'var(--text-secondary)', marginTop: '4px', marginBottom: '12px' }}>
          Hooks can rewrite a tool call after you approve it. Choose what your approval covers.
        </p>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
          <label style={{ display: 'flex', gap: '10px', alignItems: 'flex-start', cursor: 'pointer' }}>
            <input
              type="radio"
              name="approval-binding-mode"
              value="rebind"
              checked={approvalBindingMode === 'rebind'}
              onChange={() => handleApprovalBindingChange('rebind')}
              style={{ marginTop: '3px', accentColor: 'var(--accent)', cursor: 'pointer' }}
            />
            <span>
              <span style={{ fontSize: '14px', fontWeight: 500, display: 'block' }}>Rebind approval to final arguments (strict)</span>
              <span style={{ fontSize: '13px', color: 'var(--text-secondary)', display: 'block', marginTop: '2px' }}>If a hook changes a call you approved and the changed call needs approval, you are asked again on the final version.</span>
            </span>
          </label>
          <label style={{ display: 'flex', gap: '10px', alignItems: 'flex-start', cursor: 'pointer' }}>
            <input
              type="radio"
              name="approval-binding-mode"
              value="carry"
              checked={approvalBindingMode === 'carry'}
              onChange={() => handleApprovalBindingChange('carry')}
              style={{ marginTop: '3px', accentColor: 'var(--accent)', cursor: 'pointer' }}
            />
            <span>
              <span style={{ fontSize: '14px', fontWeight: 500, display: 'block' }}>Carry my approval to rewritten arguments</span>
              <span style={{ fontSize: '13px', color: 'var(--text-secondary)', display: 'block', marginTop: '2px' }}>Your approval of the original call also covers hook-rewritten versions, unless the rewrite is denied.</span>
            </span>
          </label>
        </div>
      </div>

      <div data-testid="approval-grants" style={{ marginBottom: '24px' }}>
        <h3 style={{ fontSize: '16px', fontWeight: 500, marginBottom: '4px' }}>Approval grants</h3>
        <p style={{ fontSize: '13px', color: 'var(--text-muted)', marginBottom: '12px' }}>Files and folders you approved with &quot;remember&quot; — matching tool calls run without asking until you revoke the grant. A grant covers any tool at or below its risk ceiling under the path; the tool named on each grant is the one it was created from.</p>
        {!grantsAnswered && approvalGrants.length === 0 ? (
          <LoadStateRow
            loadState="loading"
            isConnected={isConnected}
            testId="approval-grants-load-state"
            loadingText="Loading approval grants…"
            offlineText="Waiting for the engine — remembered approval grants will appear here once it connects."
            style={{ padding: '16px' }}
          />
        ) : approvalGrants.length === 0 ? (
          <div style={{ color: 'var(--text-secondary)', fontSize: '14px', padding: '16px', textAlign: 'center', background: 'var(--bg-base)', borderRadius: '12px' }}>
            No approval grants remembered.
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {approvalGrants.map(grant => (
              <div key={grant.id} style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: '12px', padding: '12px 16px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap' }}>
                  <span style={{ fontFamily: 'var(--font-mono)', fontSize: '13px' }}>{grant.prefix}</span>
                  <span style={{ fontSize: '11px', background: 'rgba(var(--accent-rgb), 0.15)', color: 'var(--accent)', padding: '2px 6px', borderRadius: '4px', marginLeft: '8px' }}>up to {grant.ceiling}</span>
                  {grant.tool_name && (
                    <span style={{ fontSize: '11px', color: 'var(--text-secondary)', marginLeft: '8px' }}>originating tool: {grant.tool_name}</span>
                  )}
                </div>
                {pendingGrantRevokeFor(grant.id) ? (
                  <span style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <button
                      disabled
                      style={{ padding: '6px 12px', borderRadius: '8px', background: 'rgba(201,42,42, 0.08)', color: 'var(--danger)', border: '1px solid rgba(201,42,42, 0.2)', cursor: 'default', fontSize: '12px', fontWeight: 500, opacity: 0.55 }}
                    >
                      Revoke queued
                    </button>
                    <span style={{ fontSize: '11px', color: 'var(--accent)' }}>will apply when the engine reconnects</span>
                  </span>
                ) : (
                  <button
                    onClick={() => handleRevokeGrant(grant.id)}
                    style={{ padding: '6px 12px', borderRadius: '8px', background: 'rgba(201,42,42, 0.15)', color: 'var(--danger)', border: '1px solid rgba(201,42,42, 0.35)', cursor: 'pointer', fontSize: '12px', fontWeight: 500 }}
                  >
                    Revoke
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* RUX-C-06: the path-rules editor is config-driven —
          before the config answer it showed "No path rules."
          for a config that simply had not loaded yet. */}
      {!configAnswered ? (
        <LoadStateRow
          loadState="loading"
          isConnected={isConnected}
          testId="path-rules-load-state"
          loadingText="Loading path rules…"
          offlineText="Waiting for the engine — your path rules will appear here once it connects."
        />
      ) : (
        <PathRulesPanel rules={engineConfig ? engineConfig.path_rules : undefined} configError={configUpdateError} onConfigUpdate={onConfigUpdate} />
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
        {!toolsAnswered && tools.length === 0 ? (
          <LoadStateRow
            loadState="loading"
            isConnected={isConnected}
            testId="tools-load-state"
            loadingText="Loading tools…"
            offlineText="Waiting for the engine — registered tools will appear here once it connects."
          />
        ) : tools.length === 0 ? (
          <div style={{ color: 'var(--text-secondary)', fontSize: '14px', padding: '24px', textAlign: 'center', background: 'var(--bg-base)', borderRadius: '12px' }}>
            No tools registered.
          </div>
        ) : (
          tools.map(tool => {
            const isWrite = tool.risk === 'write';
            const isDestructive = tool.risk === 'destructive';
            const isExecute = tool.risk === 'execute';
                  
            let badge = null;
            if (isWrite) {
              badge = <span style={{ fontSize: '11px', background: 'rgba(var(--accent-rgb), 0.2)', color: 'var(--accent)', padding: '2px 6px', borderRadius: '4px', marginLeft: '8px' }}>Write</span>;
            } else if (isDestructive || isExecute) {
              badge = <span style={{ fontSize: '11px', background: 'rgba(201,42,42, 0.2)', color: 'var(--danger)', padding: '2px 6px', borderRadius: '4px', marginLeft: '8px', display: 'inline-flex', alignItems: 'center', gap: '4px' }}><Lock size={10} /> Risky</span>;
            }

            // Phase 10.3 C2: the old 'default' option is gone. It was
            // un-sendable — the handler returned early on it, and no
            // engine message exists to clear a stored override (TOOL_POLICY_SET_REQUEST only stores;
            // policy_is_default means "no entry in tool_policies", and
            // there is no reset event in the contracts). So the
            // dropdown now lists only the three policies it can
            // actually send, and when the tool is at its default the
            // dropdown shows the *effective* policy the engine
            // resolves for it, with a note that no override is set.
            const effectivePolicyValue =
              tool.effective_policy === 'allow' ? 'Always Allow'
              : tool.effective_policy === 'deny' ? 'Deny'
              : 'Require Approval';

            const options = [
              { value: 'Always Allow', label: 'Always Allow', disabled: !tool.always_allow_permitted },
              { value: 'Require Approval', label: 'Require Approval' },
              { value: 'Deny', label: 'Deny' }
            ];

            return (
              <div key={tool.function.name} style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: '12px', padding: '16px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div>
                  <h4 style={{ fontSize: '15px', fontWeight: 500, display: 'flex', alignItems: 'center' }}>
                    {tool.function.name}
                    {badge}
                  </h4>
                  <p style={{ fontSize: '13px', color: 'var(--text-muted)', marginTop: '4px' }}>{tool.function.description}</p>
                  {tool.policy_is_default && (
                     <p style={{ fontSize: '11px', color: 'var(--text-secondary)', marginTop: '4px' }}>Default policy — no override set. Pick one to override it.</p>
                  )}
                  {!tool.always_allow_permitted && (
                     <p style={{ fontSize: '11px', color: 'var(--accent)', marginTop: '4px' }}>Always Allow is disabled for risky tools.</p>
                  )}
                </div>
                <div>
                  {(() => {
                    // RUX-A-10: the dropdown is controlled from
                    // the engine's tool list, so a discarded
                    // change snapped back with no explanation —
                    // and a user could believe a tool restricted
                    // when it was not. Through safeSend the pick
                    // QUEUES while offline; while THIS tool's
                    // change is queued its dropdown is inert and
                    // names the pending policy, and the reply
                    // drives the control when it lands.
                    const pendingPolicy = pendingToolPolicyFor(tool.function.name);
                    return (
                      <>
                        <div style={{ opacity: pendingPolicy ? 0.55 : 1, pointerEvents: pendingPolicy ? 'none' : 'auto' }}>
                          <Dropdown
                            value={tool.policy_is_default ? effectivePolicyValue : tool.policy}
                            onChange={(val) => onToolPolicyChange(tool.function.name, val)}
                            options={options}
                            style={{ width: '220px' }}
                            align="right"
                          />
                        </div>
                        {pendingPolicy && (
                          <div style={{ fontSize: '12px', color: 'var(--accent)', marginTop: '8px', width: '220px' }}>
                            Policy change to {pendingPolicy.payload.policy} queued — will apply when the engine reconnects.
                          </div>
                        )}
                      </>
                    );
                  })()}
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
