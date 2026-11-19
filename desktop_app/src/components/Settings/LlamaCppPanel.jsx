import React, { useState, useEffect } from 'react';
import { Eject, Play, RefreshCw, Square } from 'lucide-react';
import { useAetherSocket } from '../../contexts/SocketContext';
import LoadStateRow from './LoadStateRow';
import FitBadge, { LoadStateBadge } from '../FitBadge';
import { formatBytes } from '../../formatBytes';

// The llama.cpp router section of the Local Models tab: the
// second local serving backend, beside Ollama. The router
// serves models on this machine (loopback, port 9931 by
// default); Aether's presets say which models it keeps ready —
// some always loaded, everyday ones loaded when needed, heavy
// ones loaded for a job and unloaded after.
//
// The panel owns its reads and writes over the LLAMACPP_* pairs,
// following the Profile/Workflows discipline: what is on screen
// changes only when the engine's reply confirms it, a refusal
// shows the engine's reason verbatim, and a failed read is an
// error state — never an empty list dressed as an answer. There
// is no interval poller (no neighbouring panel has one): reads
// happen on mount, on the Refresh button, and after an action
// the engine confirmed.

const TIER_LABELS = {
  resident: 'Always loaded',
  daily: 'Everyday — loads when needed',
  heavy: 'Heavy — loads for a job, then unloads',
};

// "Preset name · tier words" for a model row or a preset row:
// whichever of the two the engine reported, in plain words. An
// unrecognised tier is not rendered — an unknown label would be
// a guess, and the row still carries its name.
function tierLine(preset, tier) {
  const parts = [];
  if (preset) parts.push(preset);
  if (tier && TIER_LABELS[tier]) parts.push(TIER_LABELS[tier]);
  return parts.join(' · ');
}

export default function LlamaCppPanel() {
  const { wsRef, sendMessage, isConnected } = useAetherSocket();
  const [statusLoad, setStatusLoad] = useState('loading');
  const [statusError, setStatusError] = useState(null);
  const [status, setStatus] = useState(null);
  const [modelsLoad, setModelsLoad] = useState('loading');
  const [modelsError, setModelsError] = useState(null);
  const [models, setModels] = useState([]);
  const [presets, setPresets] = useState([]);
  const [pendingModelId, setPendingModelId] = useState(null);
  const [modelActionError, setModelActionError] = useState(null);
  const [controlPending, setControlPending] = useState(false);
  const [controlError, setControlError] = useState(null);

  const requestStatus = () => {
    sendMessage('LLAMACPP_STATUS_REQUEST', {
      schema_version: 1, request_id: Date.now().toString(), payload: {},
    });
  };

  const requestModels = () => {
    sendMessage('LLAMACPP_MODELS_REQUEST', {
      schema_version: 1, request_id: Date.now().toString(), payload: {},
    });
  };

  const refresh = () => {
    setStatusLoad('loading');
    setModelsLoad('loading');
    requestStatus();
    requestModels();
  };

  useEffect(() => {
    requestStatus();
    requestModels();

    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'LLAMACPP_STATUS_RESPONSE') {
          if (data.payload.success === false || data.payload.error) {
            setStatusError(data.payload.error || 'The router status could not be loaded.');
            setStatusLoad('error');
          } else {
            setStatusError(null);
            setStatus(data.payload);
            setStatusLoad('ready');
          }
        } else if (data.type === 'LLAMACPP_MODELS_RESPONSE') {
          if (data.payload.success === false || data.payload.error) {
            setModelsError(data.payload.error || 'The router models could not be loaded.');
            setModelsLoad('error');
          } else {
            setModelsError(null);
            setModels(Array.isArray(data.payload.models) ? data.payload.models : []);
            setPresets(Array.isArray(data.payload.presets) ? data.payload.presets : []);
            setModelsLoad('ready');
          }
        } else if (data.type === 'LLAMACPP_MODEL_LOAD_RESPONSE'
          || data.type === 'LLAMACPP_MODEL_UNLOAD_RESPONSE') {
          setPendingModelId(null);
          if (data.payload.success === false || data.payload.error) {
            // A refusal leaves every row as it was and says why,
            // in the engine's own words.
            setModelActionError(data.payload.error || 'The router refused the change.');
          } else {
            setModelActionError(null);
            requestModels();
          }
        } else if (data.type === 'LLAMACPP_ROUTER_CONTROL_RESPONSE') {
          setControlPending(false);
          if (data.payload.success === false || data.payload.error) {
            setControlError(data.payload.error || 'The router control was refused.');
          } else {
            setControlError(null);
            refresh();
          }
        }
      } catch (e) {
        // ignore
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
    // Mount-only fetch plus explicit refresh/action sends, the
    // Profile panel's discipline.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wsRef, sendMessage]);

  const handleModelAction = (type, model) => {
    setModelActionError(null);
    setPendingModelId(model.id);
    sendMessage(type, {
      schema_version: 1, request_id: Date.now().toString(),
      payload: { model_id: model.id },
    });
  };

  const handleControl = (action) => {
    setControlError(null);
    setControlPending(true);
    sendMessage('LLAMACPP_ROUTER_CONTROL_REQUEST', {
      schema_version: 1, request_id: Date.now().toString(),
      payload: { action },
    });
  };

  const renderModelRow = (model) => {
    const pending = pendingModelId === model.id;
    const sizeText = model.size_bytes ? formatBytes(model.size_bytes) : null;
    const presetText = tierLine(model.preset, model.tier);
    const subLine = [sizeText, presetText].filter(Boolean).join(' · ');
    return (
      <div
        key={model.id}
        data-testid={`llamacpp-model-${model.id}`}
        style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '12px', padding: '10px 14px', background: 'var(--bg-base)', border: '1px solid var(--border-default)', borderRadius: '12px' }}
      >
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: '14px', fontWeight: 500, color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {model.name}
          </div>
          {subLine && (
            <div style={{ fontSize: '12px', color: 'var(--text-muted)' }}>{subLine}</div>
          )}
          <div style={{ display: 'flex', gap: '6px', marginTop: '6px', flexWrap: 'wrap', alignItems: 'center' }}>
            {/* The wire carries the fit verdict as a bare tier
                string; the shared badge takes the {tier, reason}
                object the hub rows carry — adapted here, rendered
                by the same component either way. */}
            <FitBadge fit={model.fit ? { tier: model.fit } : null} testId={`llamacpp-fit-${model.id}`} />
            <LoadStateBadge loaded={model.loaded} testId={`llamacpp-load-state-${model.id}`} />
            {model.loaded !== true && model.loaded !== false && (
              <span data-testid={`llamacpp-load-unknown-${model.id}`} style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
                Load state unknown
              </span>
            )}
          </div>
        </div>
        <div style={{ display: 'flex', gap: '8px', flexShrink: 0 }}>
          {model.loaded === true && (
            <button
              onClick={() => handleModelAction('LLAMACPP_MODEL_UNLOAD_REQUEST', model)}
              disabled={pending}
              data-testid={`llamacpp-unload-${model.id}`}
              className="aether-solid-btn"
              title="Unload from the router's memory (the files stay on disk)"
              style={{ padding: '8px 10px', borderRadius: '10px', cursor: pending ? 'default' : 'pointer', opacity: pending ? 0.45 : 1, display: 'inline-flex', alignItems: 'center', gap: '6px', fontSize: '12px' }}
            >
              <Eject size={14} /> {pending ? 'Unloading…' : 'Unload'}
            </button>
          )}
          {model.loaded === false && (
            <button
              onClick={() => handleModelAction('LLAMACPP_MODEL_LOAD_REQUEST', model)}
              disabled={pending}
              data-testid={`llamacpp-load-${model.id}`}
              className="aether-solid-btn"
              title="Load into the router's memory"
              style={{ padding: '8px 10px', borderRadius: '10px', cursor: pending ? 'default' : 'pointer', opacity: pending ? 0.45 : 1, display: 'inline-flex', alignItems: 'center', gap: '6px', fontSize: '12px' }}
            >
              <Play size={14} /> {pending ? 'Loading…' : 'Load'}
            </button>
          )}
        </div>
      </div>
    );
  };

  const renderPresets = () => {
    if (modelsLoad !== 'ready' || presets.length === 0) return null;
    return (
      <div data-testid="llamacpp-presets" style={{ marginTop: '20px' }}>
        <h4 style={{ fontSize: '14px', fontWeight: 600, margin: '0 0 4px 0' }}>Presets</h4>
        <p style={{ color: 'var(--text-secondary)', fontSize: '12px', margin: '0 0 12px 0', lineHeight: '1.5' }}>
          The model line-up Aether keeps ready on the router. Getting a preset onto this machine happens in the model search above — this list only reports what the presets are and whether they are here.
        </p>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
          {presets.map((preset) => (
            <div
              key={preset.id}
              data-testid={`llamacpp-preset-${preset.id}`}
              style={{ padding: '10px 14px', background: 'var(--bg-base)', border: '1px solid var(--border-default)', borderRadius: '12px' }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
                <span style={{ fontSize: '14px', fontWeight: 500, color: 'var(--text-primary)' }}>{preset.name}</span>
                {tierLine(null, preset.tier) && (
                  <span style={{ fontSize: '11px', color: 'var(--text-secondary)' }}>{tierLine(null, preset.tier)}</span>
                )}
                {preset.available === false && (
                  <span data-testid={`llamacpp-preset-unavailable-${preset.id}`} style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
                    Not on this machine yet
                  </span>
                )}
              </div>
              {preset.description && (
                <div style={{ fontSize: '12px', color: 'var(--text-muted)', marginTop: '4px', lineHeight: '1.5' }}>{preset.description}</div>
              )}
            </div>
          ))}
        </div>
      </div>
    );
  };

  const renderBody = () => {
    if (statusLoad !== 'ready') {
      return (
        <LoadStateRow
          loadState={statusLoad}
          isConnected={isConnected}
          testId="llamacpp-load-state"
          loadingText="Checking the llama.cpp router…"
          offlineText="Waiting for the engine — the router status will appear here once it connects."
          errorText={statusError || 'The router status could not be loaded.'}
        />
      );
    }

    if (!status.enabled) {
      return (
        <div data-testid="llamacpp-off-state">
          <div style={{ fontSize: '14px', fontWeight: 500, color: 'var(--text-primary)', marginBottom: '4px' }}>
            The llama.cpp router is off
          </div>
          <p style={{ color: 'var(--text-secondary)', fontSize: '13px', margin: 0, lineHeight: '1.5' }}>
            Local models can be served by the llama.cpp router running on this machine instead of Ollama. Turning it on happens in Aether&apos;s config — it isn&apos;t switched from here.
          </p>
        </div>
      );
    }

    if (!status.running) {
      return (
        <>
          <div data-testid="llamacpp-not-running">
            <div style={{ fontSize: '14px', fontWeight: 500, color: 'var(--text-primary)', marginBottom: '4px' }}>
              Router not running
            </div>
            <p style={{ color: 'var(--text-secondary)', fontSize: '13px', margin: '0 0 12px 0', lineHeight: '1.5' }}>
              Aether tried {status.url} — nothing answered. Start the router to serve local models from it.
            </p>
            <button
              onClick={() => handleControl('start')}
              disabled={controlPending}
              data-testid="llamacpp-start-btn"
              className="aether-solid-btn"
              style={{ padding: '8px 12px', borderRadius: '10px', cursor: controlPending ? 'default' : 'pointer', opacity: controlPending ? 0.45 : 1, display: 'inline-flex', alignItems: 'center', gap: '6px', fontSize: '12px' }}
            >
              <Play size={14} /> {controlPending ? 'Starting…' : 'Start router'}
            </button>
            {controlError && (
              <div data-testid="llamacpp-control-error" role="alert" style={{ fontSize: '13px', color: 'var(--danger)', marginTop: '12px' }}>
                {controlError}
              </div>
            )}
          </div>
          {renderPresets()}
        </>
      );
    }

    const statusParts = [`Running at ${status.url}`];
    if (status.version) statusParts.push(`version ${status.version}`);
    if (typeof status.models_loaded === 'number') {
      statusParts.push(`${status.models_loaded} model${status.models_loaded === 1 ? '' : 's'} loaded`);
    }
    if (status.supervised) statusParts.push('Managed by Aether');

    return (
      <>
        <div data-testid="llamacpp-status" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '12px', marginBottom: '16px' }}>
          <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>{statusParts.join(' · ')}</div>
          {status.supervised && (
            <button
              onClick={() => handleControl('stop')}
              disabled={controlPending}
              data-testid="llamacpp-stop-btn"
              className="aether-solid-btn"
              title="Stop the router Aether started"
              style={{ padding: '8px 10px', borderRadius: '10px', cursor: controlPending ? 'default' : 'pointer', opacity: controlPending ? 0.45 : 1, display: 'inline-flex', alignItems: 'center', gap: '6px', fontSize: '12px', flexShrink: 0 }}
            >
              <Square size={13} /> {controlPending ? 'Stopping…' : 'Stop router'}
            </button>
          )}
        </div>
        {controlError && (
          <div data-testid="llamacpp-control-error" role="alert" style={{ fontSize: '13px', color: 'var(--danger)', marginBottom: '12px' }}>
            {controlError}
          </div>
        )}
        {modelActionError && (
          <div data-testid="llamacpp-action-error" role="alert" style={{ fontSize: '13px', color: 'var(--danger)', marginBottom: '12px' }}>
            {modelActionError}
          </div>
        )}
        {modelsLoad !== 'ready' ? (
          <LoadStateRow
            loadState={modelsLoad}
            isConnected={isConnected}
            testId="llamacpp-models-load-state"
            loadingText="Loading the router's models…"
            offlineText="Waiting for the engine — the router's models will appear here once it connects."
            errorText={modelsError || 'The router models could not be loaded.'}
          />
        ) : models.length === 0 ? (
          <div style={{ color: 'var(--text-secondary)', fontSize: '14px' }}>
            The router reports no models on this machine yet.
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {models.map(renderModelRow)}
          </div>
        )}
        {renderPresets()}
      </>
    );
  };

  return (
    <div
      data-testid="llamacpp-panel"
      style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '20px', marginTop: '24px' }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '12px', marginBottom: '8px' }}>
        <h3 style={{ fontSize: '16px', fontWeight: 600, margin: 0, color: 'var(--text-primary)' }}>llama.cpp router</h3>
        <button
          onClick={refresh}
          data-testid="llamacpp-refresh"
          className="aether-solid-btn"
          style={{ padding: '6px 12px', fontSize: '12px', display: 'inline-flex', alignItems: 'center', gap: '6px', cursor: 'pointer', color: 'var(--text-primary)', border: '1px solid var(--border-default)', borderRadius: '10px' }}
        >
          <RefreshCw size={13} /> Refresh
        </button>
      </div>
      <p style={{ color: 'var(--text-secondary)', fontSize: '13px', margin: '0 0 16px 0', lineHeight: '1.5' }}>
        Local models served by the llama.cpp router on this machine. Everyday models stay ready; heavy ones load only for the job that needs them and unload after.
      </p>
      {renderBody()}
    </div>
  );
}
