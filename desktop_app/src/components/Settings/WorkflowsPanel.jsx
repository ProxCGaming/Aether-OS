import React, { useState, useEffect } from 'react';
import { ArrowDown, ArrowUp, Pencil, Plus, Square, Trash2, Workflow } from 'lucide-react';
import { useAetherSocket } from '../../contexts/SocketContext';
import LoadStateRow from './LoadStateRow';
import EmptyState from '../EmptyState';

// Workflows manager: the saved step sequences behind the
// Dashboard's "Start New Workflow" picker. A workflow is a name,
// a description and an ordered list of steps, each step one task
// prompt; running one (from the Dashboard) runs the steps in
// order as ordinary tasks in one chat session.
//
// The panel owns its reads and writes over the WORKFLOW_* pairs,
// following the Profile panel's discipline: the list on screen
// changes only when the engine's reply confirms the change, a
// refusal shows the engine's reason verbatim and leaves the
// editor open, and a failed read is an error state — never an
// empty list dressed as an answer. The run strip is a live read
// model: WORKFLOW_RUN_LIST answers on open, WORKFLOW_RUN_STATUS
// events upsert by run id as runs move.

const RUN_STATUS_LABELS = {
  running: 'Running',
  awaiting_approval: 'Waiting for your approval',
  completed: 'Completed',
  failed: 'Failed',
  stopped: 'Stopped',
  interrupted: 'Interrupted by restart',
};

const ACTIVE_RUN_STATUSES = new Set(['running', 'awaiting_approval']);

// "Step N of M — <title>" for a run: the engine's current_step
// index when it points at a real step, otherwise the step the
// run is sitting on (the one marked running / awaiting). A run
// with neither has no current step to name.
function runStepLine(run) {
  const steps = Array.isArray(run.steps) ? run.steps : [];
  if (steps.length === 0) return null;
  let idx = -1;
  if (typeof run.current_step === 'number' && steps[run.current_step]) {
    idx = run.current_step;
  } else {
    idx = steps.findIndex((s) => s.status === 'running' || s.status === 'awaiting_approval');
  }
  if (idx === -1) return null;
  return `Step ${idx + 1} of ${steps.length} — ${steps[idx].title || 'Untitled step'}`;
}

// The client-side save gate, mirroring the engine's validation:
// the user learns about a blank name, an empty step list or a
// blank step prompt here, not after a round trip.
function editorProblem(editor) {
  if (!editor.name.trim()) return 'Give the workflow a name to save it.';
  if (editor.steps.length === 0) return 'Add at least one step.';
  if (editor.steps.some((s) => !s.prompt.trim())) {
    return 'Every step needs a prompt — fill in the blank step or remove it.';
  }
  return null;
}

export default function WorkflowsPanel() {
  const { wsRef, sendMessage, isConnected } = useAetherSocket();
  const [listLoad, setListLoad] = useState('loading');
  const [listError, setListError] = useState(null);
  const [workflows, setWorkflows] = useState([]);
  const [runs, setRuns] = useState([]);
  const [editor, setEditor] = useState(null);
  const [saveError, setSaveError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [actionError, setActionError] = useState(null);
  const [stopError, setStopError] = useState(null);

  const requestList = () => {
    sendMessage('WORKFLOW_LIST_REQUEST', {
      schema_version: 1, request_id: Date.now().toString(), payload: {},
    });
  };

  const requestRuns = () => {
    sendMessage('WORKFLOW_RUN_LIST_REQUEST', {
      schema_version: 1, request_id: Date.now().toString(), payload: {},
    });
  };

  useEffect(() => {
    requestList();
    requestRuns();

    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'WORKFLOW_LIST_RESPONSE') {
          if (data.payload.success === false || data.payload.error) {
            setListError(data.payload.error || 'Your workflows could not be loaded.');
            setListLoad('error');
          } else {
            setListError(null);
            setWorkflows(Array.isArray(data.payload.workflows) ? data.payload.workflows : []);
            setListLoad('ready');
          }
        } else if (data.type === 'WORKFLOW_SAVE_RESPONSE') {
          setSaving(false);
          if (data.payload.success === false || data.payload.error) {
            // A refusal keeps the editor open with the engine's
            // reason verbatim; nothing on screen was changed.
            setSaveError(data.payload.error || 'The workflow could not be saved.');
          } else {
            setSaveError(null);
            setEditor(null);
            requestList();
          }
        } else if (data.type === 'WORKFLOW_DELETE_RESPONSE') {
          if (data.payload.success === false || data.payload.error) {
            setActionError(data.payload.error || 'The workflow could not be deleted.');
          } else {
            setActionError(null);
            requestList();
          }
        } else if (data.type === 'WORKFLOW_RUN_LIST_RESPONSE') {
          if (!(data.payload.success === false || data.payload.error)) {
            setRuns(Array.isArray(data.payload.runs) ? data.payload.runs : []);
          }
        } else if (data.type === 'WORKFLOW_RUN_STATUS') {
          const run = data.payload;
          if (run && run.run_id) {
            setRuns((prev) => {
              const idx = prev.findIndex((r) => r.run_id === run.run_id);
              if (idx === -1) return [run, ...prev];
              const next = [...prev];
              next[idx] = run;
              return next;
            });
          }
        } else if (data.type === 'WORKFLOW_RUN_STOP_RESPONSE') {
          if (data.payload.success === false || data.payload.error) {
            setStopError(data.payload.error || 'The run could not be stopped.');
          } else {
            setStopError(null);
            requestRuns();
          }
        }
      } catch (e) {
        // ignore malformed frames
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wsRef, sendMessage]);

  const openNewEditor = () => {
    setSaveError(null);
    setEditor({ name: '', description: '', steps: [{ title: '', prompt: '' }] });
  };

  const openEditEditor = (workflow) => {
    setSaveError(null);
    setEditor({
      id: workflow.id,
      name: workflow.name || '',
      description: workflow.description || '',
      steps: (Array.isArray(workflow.steps) ? workflow.steps : []).map((s) => ({
        title: s.title || '',
        prompt: s.prompt || '',
      })),
    });
  };

  const updateStep = (idx, field, value) => {
    setEditor((prev) => {
      const steps = prev.steps.map((s, i) => (i === idx ? { ...s, [field]: value } : s));
      return { ...prev, steps };
    });
  };

  const addStep = () => {
    setEditor((prev) => ({ ...prev, steps: [...prev.steps, { title: '', prompt: '' }] }));
  };

  const removeStep = (idx) => {
    setEditor((prev) => ({ ...prev, steps: prev.steps.filter((_, i) => i !== idx) }));
  };

  const moveStep = (idx, delta) => {
    setEditor((prev) => {
      const target = idx + delta;
      if (target < 0 || target >= prev.steps.length) return prev;
      const steps = [...prev.steps];
      const [moved] = steps.splice(idx, 1);
      steps.splice(target, 0, moved);
      return { ...prev, steps };
    });
  };

  const handleSave = () => {
    if (!editor || editorProblem(editor) || saving) return;
    setSaving(true);
    setSaveError(null);
    const workflow = {
      ...(editor.id ? { id: editor.id } : {}),
      name: editor.name,
      description: editor.description,
      steps: editor.steps.map((s) => ({ title: s.title, prompt: s.prompt })),
    };
    sendMessage('WORKFLOW_SAVE_REQUEST', {
      schema_version: 1, request_id: Date.now().toString(), payload: { workflow },
    });
  };

  const handleDelete = (workflow) => {
    if (!window.confirm(`Delete the workflow "${workflow.name}"? The workflow and its steps will be deleted. This cannot be undone.`)) return;
    setActionError(null);
    sendMessage('WORKFLOW_DELETE_REQUEST', {
      schema_version: 1, request_id: Date.now().toString(), payload: { workflow_id: workflow.id },
    });
  };

  const handleStopRun = (run) => {
    setStopError(null);
    sendMessage('WORKFLOW_RUN_STOP_REQUEST', {
      schema_version: 1, request_id: Date.now().toString(), payload: { run_id: run.run_id },
    });
  };

  const problem = editor ? editorProblem(editor) : null;
  const workflowNameFor = (run) => {
    const match = workflows.find((w) => w.id === run.workflow_id);
    return match ? match.name : 'Workflow';
  };

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
        <h2 style={{ fontSize: '20px', fontWeight: 500, margin: 0 }}>Workflows</h2>
        {!editor && (
          <button
            type="button"
            onClick={openNewEditor}
            style={{ display: 'flex', alignItems: 'center', gap: '6px', background: 'var(--accent)', border: '1px solid var(--accent)', color: 'var(--accent-on)', padding: '8px 16px', borderRadius: '10px', fontSize: '13px', fontWeight: 500, cursor: 'pointer' }}
          >
            <Plus size={15} /> New workflow
          </button>
        )}
      </div>
      <p style={{ color: 'var(--text-secondary)', fontSize: '13px', margin: '0 0 20px 0', lineHeight: '1.5' }}>
        A workflow is a saved list of steps, each one a task prompt. Running it from the Dashboard runs the steps in order, each step building on the last step's result.
      </p>

      {runs.length > 0 && (
        <div data-testid="workflow-runs" style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '16px 20px', marginBottom: '24px' }}>
          <h3 style={{ fontSize: '14px', fontWeight: 500, margin: '0 0 12px 0' }}>Runs</h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
            {runs.map((run) => {
              const stepLine = runStepLine(run);
              const isActive = ACTIVE_RUN_STATUSES.has(run.status);
              return (
                <div key={run.run_id} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '12px' }}>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: '14px', fontWeight: 500 }}>{workflowNameFor(run)}</div>
                    <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '2px' }}>
                      {RUN_STATUS_LABELS[run.status] || run.status}
                      {stepLine ? ` · ${stepLine}` : ''}
                    </div>
                  </div>
                  {isActive && (
                    <button
                      type="button"
                      onClick={() => handleStopRun(run)}
                      style={{ display: 'flex', alignItems: 'center', gap: '6px', background: 'transparent', color: 'var(--danger)', border: '1px solid rgba(201,42,42,0.4)', padding: '6px 12px', borderRadius: '8px', fontSize: '12px', cursor: 'pointer', flexShrink: 0 }}
                    >
                      <Square size={12} /> Stop
                    </button>
                  )}
                </div>
              );
            })}
          </div>
          {stopError && (
            <div style={{ color: 'var(--danger)', fontSize: '13px', marginTop: '12px' }}>{stopError}</div>
          )}
        </div>
      )}

      {editor ? (
        <div data-testid="workflow-editor" style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '20px' }}>
          <h3 style={{ fontSize: '16px', fontWeight: 500, margin: '0 0 16px 0' }}>{editor.id ? 'Edit workflow' : 'New workflow'}</h3>
          <label style={{ display: 'block', fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '6px' }} htmlFor="workflow-name">Name</label>
          <input
            id="workflow-name"
            type="text"
            value={editor.name}
            onChange={(e) => setEditor((prev) => ({ ...prev, name: e.target.value }))}
            placeholder="Morning research"
            style={{ width: '100%', boxSizing: 'border-box', background: 'var(--bg-base)', border: '1px solid var(--border-default)', borderRadius: '8px', color: 'var(--text-primary)', padding: '8px 12px', fontSize: '14px', marginBottom: '16px', outline: 'none' }}
          />
          <label style={{ display: 'block', fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '6px' }} htmlFor="workflow-description">Description</label>
          <textarea
            id="workflow-description"
            value={editor.description}
            onChange={(e) => setEditor((prev) => ({ ...prev, description: e.target.value }))}
            placeholder="What this workflow is for (optional)"
            rows={2}
            style={{ width: '100%', boxSizing: 'border-box', background: 'var(--bg-base)', border: '1px solid var(--border-default)', borderRadius: '8px', color: 'var(--text-primary)', padding: '8px 12px', fontSize: '14px', marginBottom: '20px', outline: 'none', resize: 'vertical' }}
          />

          <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
            {editor.steps.map((step, idx) => (
              <div key={idx} style={{ border: '1px solid var(--border-default)', borderRadius: '8px', padding: '14px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '10px' }}>
                  <span style={{ fontSize: '13px', fontWeight: 500 }}>Step {idx + 1}</span>
                  <div style={{ display: 'flex', gap: '4px' }}>
                    <button type="button" aria-label={`Move step ${idx + 1} up`} onClick={() => moveStep(idx, -1)} disabled={idx === 0} style={{ background: 'transparent', border: '1px solid var(--border-default)', borderRadius: '6px', color: 'var(--text-secondary)', padding: '4px 6px', cursor: idx === 0 ? 'default' : 'pointer', opacity: idx === 0 ? 0.4 : 1 }}>
                      <ArrowUp size={13} />
                    </button>
                    <button type="button" aria-label={`Move step ${idx + 1} down`} onClick={() => moveStep(idx, 1)} disabled={idx === editor.steps.length - 1} style={{ background: 'transparent', border: '1px solid var(--border-default)', borderRadius: '6px', color: 'var(--text-secondary)', padding: '4px 6px', cursor: idx === editor.steps.length - 1 ? 'default' : 'pointer', opacity: idx === editor.steps.length - 1 ? 0.4 : 1 }}>
                      <ArrowDown size={13} />
                    </button>
                    <button type="button" aria-label={`Remove step ${idx + 1}`} onClick={() => removeStep(idx)} style={{ background: 'transparent', border: '1px solid var(--border-default)', borderRadius: '6px', color: 'var(--danger)', padding: '4px 6px', cursor: 'pointer' }}>
                      <Trash2 size={13} />
                    </button>
                  </div>
                </div>
                <input
                  type="text"
                  aria-label={`Step ${idx + 1} title`}
                  value={step.title}
                  onChange={(e) => updateStep(idx, 'title', e.target.value)}
                  placeholder="Step title (optional)"
                  style={{ width: '100%', boxSizing: 'border-box', background: 'var(--bg-base)', border: '1px solid var(--border-default)', borderRadius: '8px', color: 'var(--text-primary)', padding: '8px 12px', fontSize: '13px', marginBottom: '8px', outline: 'none' }}
                />
                <textarea
                  aria-label={`Step ${idx + 1} prompt`}
                  value={step.prompt}
                  onChange={(e) => updateStep(idx, 'prompt', e.target.value)}
                  placeholder="The task prompt for this step"
                  rows={3}
                  style={{ width: '100%', boxSizing: 'border-box', background: 'var(--bg-base)', border: '1px solid var(--border-default)', borderRadius: '8px', color: 'var(--text-primary)', padding: '8px 12px', fontSize: '13px', outline: 'none', resize: 'vertical' }}
                />
              </div>
            ))}
          </div>

          <button
            type="button"
            onClick={addStep}
            style={{ display: 'flex', alignItems: 'center', gap: '6px', background: 'transparent', color: 'var(--accent)', border: '1px solid rgba(var(--accent-rgb),0.4)', padding: '8px 14px', borderRadius: '8px', fontSize: '13px', cursor: 'pointer', marginTop: '16px' }}
          >
            <Plus size={14} /> Add step
          </button>

          {saveError && (
            <div style={{ color: 'var(--danger)', fontSize: '13px', marginTop: '16px' }}>{saveError}</div>
          )}
          {problem && (
            <div style={{ color: 'var(--text-secondary)', fontSize: '13px', marginTop: '16px' }}>{problem}</div>
          )}
          <div style={{ display: 'flex', gap: '8px', marginTop: '20px' }}>
            <button
              type="button"
              onClick={handleSave}
              disabled={!!problem || saving}
              style={{ background: 'var(--accent)', border: '1px solid var(--accent)', color: 'var(--accent-on)', padding: '9px 18px', borderRadius: '10px', fontSize: '13px', fontWeight: 500, cursor: (!!problem || saving) ? 'default' : 'pointer', opacity: (!!problem || saving) ? 0.5 : 1 }}
            >
              {saving ? 'Saving…' : 'Save workflow'}
            </button>
            <button
              type="button"
              onClick={() => { setEditor(null); setSaveError(null); }}
              style={{ background: 'transparent', border: '1px solid var(--border-default)', color: 'var(--text-primary)', padding: '9px 18px', borderRadius: '10px', fontSize: '13px', cursor: 'pointer' }}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : listLoad !== 'ready' ? (
        <LoadStateRow
          loadState={listLoad}
          isConnected={isConnected}
          testId="workflows-load-state"
          loadingText="Loading your workflows…"
          offlineText="Waiting for the engine — your workflows will appear here once it connects."
          errorText={listError || 'Your workflows could not be loaded.'}
        />
      ) : workflows.length === 0 ? (
        <EmptyState
          icon={<Workflow size={22} color="var(--text-muted)" />}
          title="No workflows yet"
          description="Create your first workflow: a named list of steps that run in order as tasks."
          actionLabel="Create your first workflow"
          onAction={openNewEditor}
          testId="workflows-empty"
        />
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
          {actionError && (
            <div style={{ color: 'var(--danger)', fontSize: '13px' }}>{actionError}</div>
          )}
          {workflows.map((workflow) => (
            <div key={workflow.id} style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '16px 20px', display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '12px' }}>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: '15px', fontWeight: 500 }}>{workflow.name}</div>
                <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '2px' }}>
                  {Array.isArray(workflow.steps) ? workflow.steps.length : 0} {Array.isArray(workflow.steps) && workflow.steps.length === 1 ? 'step' : 'steps'}
                </div>
                {workflow.description && (
                  <div style={{ fontSize: '13px', color: 'var(--text-secondary)', marginTop: '4px' }}>{workflow.description}</div>
                )}
              </div>
              <div style={{ display: 'flex', gap: '8px', flexShrink: 0 }}>
                <button
                  type="button"
                  onClick={() => openEditEditor(workflow)}
                  style={{ display: 'flex', alignItems: 'center', gap: '6px', background: 'transparent', border: '1px solid var(--border-default)', color: 'var(--text-primary)', padding: '7px 12px', borderRadius: '8px', fontSize: '12px', cursor: 'pointer' }}
                >
                  <Pencil size={13} /> Edit
                </button>
                <button
                  type="button"
                  onClick={() => handleDelete(workflow)}
                  style={{ display: 'flex', alignItems: 'center', gap: '6px', background: 'transparent', border: '1px solid rgba(201,42,42,0.4)', color: 'var(--danger)', padding: '7px 12px', borderRadius: '8px', fontSize: '12px', cursor: 'pointer' }}
                >
                  <Trash2 size={13} /> Delete
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
