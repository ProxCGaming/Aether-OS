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
  'in-app': { label: 'In-app', color: '#34d399', bg: 'rgba(16, 185, 129, 0.15)', border: 'rgba(16, 185, 129, 0.3)' },
  ollama: { label: 'Via Ollama', color: '#f5b544', bg: 'rgba(245, 181, 52, 0.12)', border: 'rgba(245, 181, 52, 0.35)' },
  'not-runnable': { label: 'Not locally runnable', color: '#f87171', bg: 'rgba(239, 68, 68, 0.12)', border: 'rgba(239, 68, 68, 0.3)' },
  unknown: { label: 'Run support unknown', color: '#9090a0', bg: 'rgba(144, 144, 160, 0.12)', border: 'rgba(144, 144, 160, 0.3)' },
};

export const chipStyle = {
  background: 'rgba(100, 100, 100, 0.15)', color: 'var(--text-primary, #e2e8f0)',
  padding: '4px 8px', borderRadius: '6px', fontSize: '11px', fontWeight: 500,
};

export const downloadButtonStyle = {
  padding: '10px 16px', background: 'rgba(var(--accent-rgb, 124, 58, 237), 0.2)',
  color: 'var(--text-primary, #fff)', border: '1px solid rgba(var(--accent-rgb, 124, 58, 237), 0.5)',
  borderRadius: '12px', cursor: 'pointer', fontWeight: 500, display: 'flex',
  justifyContent: 'center', alignItems: 'center', gap: '8px', transition: 'all 0.2s',
};
