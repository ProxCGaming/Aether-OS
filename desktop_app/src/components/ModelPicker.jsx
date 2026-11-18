import React from 'react';
import Dropdown from '../Dropdown';

// Full Furnish Spec C (FF3-07): THE shared model picker. Chat, the
// Settings default-model picker, the auxiliary-model pickers and
// the per-agent pickers all used to hand-roll the same raw-id
// option lists in four places; they now render this one component
// over the shared Dropdown core, so search and metadata behave
// identically everywhere a model is chosen.
//
// What a row shows comes only from engine-supplied data — each
// provider entry may carry `model_meta` (the engine's benchmark
// cache + learned-unusable record, exposed on the provider list):
// - capability tags: Vision / Tools, from the cached capabilities;
// - a relative cost marker derived from the cached $/1M prices;
// - an "Unusable" badge for a model the engine has learned is
//   unusable — badged, never silently listed as normal and never
//   silently dropped (it stays selectable: selection behaviour at
//   every call site is unchanged, and the engine's own refusal /
//   fallback still applies exactly as before).
// A model with no metadata renders a bare row: unknown metadata is
// absent, never invented. (Context window has no engine-side
// per-model source today, so no row renders one.)
//
// Formats preserve each call site's existing value contract:
// - 'grouped' (chat, Settings default, aux): options grouped by
//   provider, values `provider:::model`, restricted to providers
//   with a key and at least one model — the filter all three sites
//   already applied.
// - 'agent' (per-agent): one flat list across all providers,
//   values `provider:model`, labels `model (Provider)` — the
//   AgentsPanel contract, including no has-key filter.

// Relative-cost bands over the cached input price ($ per 1M
// tokens): a restaurant-style marker, derived only from real
// cached prices — never from the model's name or family.
export function relativeCostLabel(meta) {
  if (!meta) return null;
  const input = meta.cost_input_1m;
  const output = meta.cost_output_1m;
  if (input == null && output == null) return null;
  if ((input ?? 0) === 0 && (output ?? 0) === 0) return 'Free';
  const basis = input != null ? input : output;
  if (basis < 0.5) return '$';
  if (basis < 3) return '$$';
  return '$$$';
}

// The row's second line: the parts that exist, joined. Undefined
// when the engine knows nothing about the model.
export function modelMetaLine(meta) {
  if (!meta) return undefined;
  const parts = [];
  const caps = Array.isArray(meta.capabilities) ? meta.capabilities : [];
  if (caps.includes('vision')) parts.push('Vision');
  if (caps.includes('tools')) parts.push('Tools');
  const cost = relativeCostLabel(meta);
  if (cost) parts.push(cost);
  return parts.length ? parts.join(' · ') : undefined;
}

function itemFor(provider, model, value, label) {
  const meta = provider.model_meta ? provider.model_meta[model] : undefined;
  const item = { value, label };
  const line = modelMetaLine(meta);
  if (line) item.meta = line;
  if (meta && meta.unusable) item.badge = { text: 'Unusable', tone: 'danger' };
  return item;
}

export function buildModelOptions(providers, format = 'grouped') {
  const list = Array.isArray(providers) ? providers : [];
  if (format === 'agent') {
    return list.flatMap(p =>
      (p.models || []).map(m =>
        itemFor(p, m, `${p.name}:${m}`, `${m} (${p.display_name || p.name})`)
      )
    );
  }
  return list
    .filter(p => p.has_key && p.models && p.models.length > 0)
    .map(p => ({
      group: p.display_name || p.name,
      items: p.models.map(m => itemFor(p, m, `${p.name}:::${m}`, m)),
    }));
}

export default function ModelPicker({
  providers = [],
  value,
  onChange,
  format = 'grouped',
  leadingOptions = [],
  placeholder,
  style,
  align,
  testId,
}) {
  const options = [...leadingOptions, ...buildModelOptions(providers, format)];
  return (
    <Dropdown
      value={value}
      onChange={onChange}
      options={options}
      placeholder={placeholder}
      style={style}
      align={align}
      testId={testId}
      searchable
      searchPlaceholder="Search models…"
      noResultsTitle="No matching models"
    />
  );
}
