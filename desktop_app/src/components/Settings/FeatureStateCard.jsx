import React, { useState, useEffect } from 'react';
import { ListChecks } from 'lucide-react';
import { useAetherSocket } from '../../contexts/SocketContext';

// Master build S21 (voice G8 / R17's Settings half): the
// per-feature state+reason card — for each feature this
// programme touched, what is active, whether it runs local or
// cloud, and why. Every state shown comes from an engine
// report or the applied config; nothing is inferred UI-side:
//
// - The capability rows render the S10 matrix (the same
//   MACHINE_CAPABILITY report the "This machine" card above
//   shows — the engine serves it from its TTL cache, so this
//   card's own request returns the same measured report; the
//   matrix card owns the hardware detail, this card owns the
//   per-feature reading of it).
// - The switch rows (memory backend, model routing, Computer
//   Use) render the applied engine config SettingsPanel hands
//   down — the same values the owning panels edit.
// - Offline mode and feature packs are NOT BUILT (owner
//   decisions O2/O3, 14 Nov: later) and the card says exactly
//   that instead of implying a mode exists.

const STATUS_STYLE = {
  available: { label: 'Available', color: 'var(--success)', border: 'rgba(76,195,138,0.4)' },
  degraded: { label: 'Degraded', color: 'var(--accent)', border: 'rgba(var(--accent-rgb),0.4)' },
  unavailable: { label: 'Unavailable', color: 'var(--danger)', border: 'rgba(201,42,42,0.4)' },
  unknown: { label: 'Unknown', color: 'var(--text-muted)', border: 'var(--border-default)' },
};

const chipStyle = (style) => ({
  fontSize: '11px', fontWeight: 600, color: style.color,
  border: `1px solid ${style.border}`, borderRadius: '999px',
  padding: '2px 10px', whiteSpace: 'nowrap',
});

const localityChipStyle = {
  fontSize: '11px', fontWeight: 600, color: 'var(--text-secondary)',
  border: '1px solid var(--border-default)', borderRadius: '999px',
  padding: '2px 10px', whiteSpace: 'nowrap',
};

const notBuiltChip = (
  <span style={chipStyle(STATUS_STYLE.unknown)}>Not built</span>
);

export default function FeatureStateCard({ config = null }) {
  const { wsRef, sendMessage, isConnected, reconnectCount = 0 } = useAetherSocket();
  const [report, setReport] = useState(null);

  useEffect(() => {
    sendMessage('MACHINE_CAPABILITY_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });

    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'MACHINE_CAPABILITY_RESPONSE') {
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
  }, [wsRef, sendMessage, reconnectCount]);

  const featureById = {};
  if (report && Array.isArray(report.features)) {
    for (const f of report.features) featureById[f.id] = f;
  }

  const matrixRow = (id, title, locality, extra) => {
    const feature = featureById[id];
    if (!feature) return null;
    const style = STATUS_STYLE[feature.status] || STATUS_STYLE.unknown;
    return (
      <div
        key={id}
        data-testid={`feature-state-${id}`}
        style={{ background: 'var(--bg-base)', border: '1px solid var(--border-default)', borderRadius: '12px', padding: '12px 16px' }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
          <span style={{ fontSize: '14px', fontWeight: 500 }}>{title}</span>
          <span style={{ display: 'flex', gap: '6px' }}>
            {locality && <span style={localityChipStyle}>{locality}</span>}
            <span data-testid={`feature-state-status-${id}`} style={chipStyle(style)}>{style.label}</span>
          </span>
        </div>
        <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '4px' }}>
          {feature.reason}
        </div>
        {extra}
      </div>
    );
  };

  const configRow = (id, title, chip, body) => (
    <div
      key={id}
      data-testid={`feature-state-${id}`}
      style={{ background: 'var(--bg-base)', border: '1px solid var(--border-default)', borderRadius: '12px', padding: '12px 16px' }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
        <span style={{ fontSize: '14px', fontWeight: 500 }}>{title}</span>
        {chip}
      </div>
      <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '4px' }}>
        {body}
      </div>
    </div>
  );

  // Computer Use's switch line reads the applied config: the
  // matrix verdict says whether this machine CAN; these say
  // whether the owner has turned each rung on.
  const desktopExtra = config ? (
    <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '4px' }}>
      {config.computer_use_enabled === true
        ? `Computer Use is on: Observe ${config.computer_use_observe === true ? 'on' : 'off'}, Control ${config.computer_use_control === true ? 'on' : 'off'} (Settings → Computer Use).`
        : 'Computer Use is off (Settings → Computer Use).'}
    </div>
  ) : null;

  const memoryBackend = config && typeof config.memory_backend === 'string' && config.memory_backend
    ? config.memory_backend
    : null;
  const routingAuto = config && config.model_routing === 'auto';

  return (
    <div
      data-testid="feature-state-card"
      style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '24px', marginTop: '20px' }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '8px' }}>
        <ListChecks size={18} style={{ color: 'var(--accent)' }} />
        <h3 style={{ fontSize: '16px', margin: 0 }}>What each feature is using</h3>
      </div>
      <p style={{ color: 'var(--text-muted)', fontSize: '13px', margin: '0 0 16px 0' }}>
        Per feature: what is active, whether it runs on this machine or in the cloud, and
        why — read from the engine&apos;s capability report and your applied settings,
        never guessed. The owning panels (named in each row) are where these change.
      </p>

      {!report && !config ? (
        <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
          {isConnected
            ? 'Asking the engine what each feature is using…'
            : 'Waiting for the engine — the per-feature state will appear here once it connects.'}
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
          {matrixRow('local_voice', 'Voice', 'Local', (
            <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '4px' }}>
              A session is planned from this verdict plus each stage&apos;s own availability
              when it starts, and steps down automatically if transcription runs slow
              (full → degraded → text-only); a running session shows its live profile in
              the voice overlay. Models and tuning: Settings → Models &amp; Tasks and
              Settings → Voice Tuning.
            </div>
          ))}
          {matrixRow('local_models', 'Local models', 'Local')}
          {matrixRow('cloud_providers', 'Cloud providers', 'Cloud')}
          {matrixRow('semantic_embeddings', 'Semantic memory embeddings', null)}
          {matrixRow('browser_automation', 'Browser automation', 'Local')}
          {matrixRow('desktop_control', 'Desktop (Computer Use)', 'Local', desktopExtra)}

          {configRow('memory',
            'Memory',
            null,
            config
              ? memoryBackend
                ? `Configured backend: ${memoryBackend}. Its live availability, warnings and switching live in Settings → Memory & Storage — a backend that cannot run says why there, and the switch is refused rather than faked.`
                : 'The applied config does not name a memory backend. Availability and switching live in Settings → Memory & Storage.'
              : 'Waiting for the applied config — the configured backend will appear here once it lands. Availability and switching live in Settings → Memory & Storage.')}

          {configRow('model_routing',
            'Model routing',
            null,
            config
              ? routingAuto
                ? 'Auto: the engine picks a model per delegation from the sub-task and each model\u2019s track record; your Agents-tab pins always win and are never escalated. The effective model per agent is listed in Settings → Agents.'
                : 'Off: specialists run on the run\u2019s model, or on the model you pinned for them in the Agents tab. The effective model per agent is listed in Settings → Agents.'
              : 'Waiting for the applied config — the routing mode will appear here once it lands. The effective model per agent is listed in Settings → Agents.')}

          {configRow('offline_mode',
            'Offline mode',
            notBuiltChip,
            'No offline mode in this build (planned, not built): nothing switches automatically when connectivity drops — local features keep working because they never needed the network, and cloud calls fail per-call as they do today.')}

          {configRow('packs',
            'Feature packs',
            notBuiltChip,
            'No on-demand feature packs in this build (planned, not built): every feature ships inside the single install, so there is no pack state to report.')}
        </div>
      )}
    </div>
  );
}
