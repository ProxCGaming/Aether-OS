// Model Hub display vocabulary (Wave 5): the label maps and shared
// style objects the catalogue (LocalModelsTab) and the downloaded
// inventory (components/DownloadedModels) both render with. They
// lived at the top of LocalModelsTab.jsx; the download-manager
// split moved the inventory out, and both sides import them from
// here rather than keeping two copies.

export const FAMILY_LABELS = {
  brain: 'Brain',
  'speech-to-text': 'Speech-to-text',
  'text-to-speech': 'Text-to-speech',
  embedding: 'Embedding',
  other: 'Other',
};

export const SOURCE_LABELS = { hf: 'Hugging Face', ollama: 'Ollama' };

// Daemon-typed model kinds (engine /api/show capabilities): what a
// downloaded model is FOR, as opposed to the hub family taxonomy.
export const TYPE_LABELS = {
  chat: 'Chat',
  embedding: 'Embedding',
  both: 'Chat + Embedding',
  unknown: 'Type unknown',
};

export const RUNNABILITY_BADGES = {
  'in-app': { label: 'In-app', color: 'var(--success)', bg: 'rgba(76, 195, 138, 0.15)', border: 'rgba(76, 195, 138, 0.3)' },
  ollama: { label: 'Via Ollama', color: 'var(--accent)', bg: 'rgba(var(--accent-rgb), 0.12)', border: 'rgba(var(--accent-rgb), 0.35)' },
  'not-runnable': { label: 'Not locally runnable', color: 'var(--danger)', bg: 'rgba(201,42,42, 0.12)', border: 'rgba(201,42,42, 0.3)' },
  unknown: { label: 'Run support unknown', color: 'var(--text-muted)', bg: 'rgba(148, 148, 143, 0.12)', border: 'rgba(148, 148, 143, 0.3)' },
};

export const chipStyle = {
  background: 'var(--bg-hover)', color: 'var(--text-secondary)',
  padding: '4px 8px', borderRadius: '6px', fontSize: '11px', fontWeight: 500,
};

export const downloadButtonStyle = {
  padding: '10px 16px', background: 'var(--accent)',
  color: 'var(--accent-on)', border: '1px solid var(--accent)',
  borderRadius: 'var(--radius-lg)', cursor: 'pointer', fontWeight: 500, display: 'flex',
  justifyContent: 'center', alignItems: 'center', gap: '8px', transition: 'all 0.2s',
};
