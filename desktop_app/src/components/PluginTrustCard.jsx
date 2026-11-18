import React from 'react';

// Wave 5 (D4-4): the ONE plugin trust card — the readable summary
// of a prepare payload, shared by the chat approval card and the
// Settings panel's in-panel install approval. Both surfaces render
// this component from the same payload ({source, manifest,
// mcp_server_summaries}); only the surrounding chrome and the
// approve/reject wiring differ (the chat card's correlation
// semantics are untouched — the decision still travels under the
// approval request's own id).
//
// The lines answer, in plain language: what the bundle ADDS
// (skills, MCP servers and what each server runs), what it CAN
// TOUCH (working folders, network hosts, environment variable and
// header NAMES — values are secrets and never appear in a prepare
// payload), what it DECLARES that cannot run (tools: a plugin
// cannot add tools directly, per the no-in-engine-code rule), and
// the package's integrity pin (the manifest's SHA-256 file map).

export function pluginTrustLines(details) {
  const manifest = (details && details.manifest) || {};
  const lines = [];

  const skills = Array.isArray(manifest.skills) ? manifest.skills : [];
  if (skills.length > 0) {
    const names = skills.map((s) => s.name).filter(Boolean).join(', ');
    lines.push({
      kind: 'adds',
      text: `Adds ${skills.length} skill${skills.length === 1 ? '' : 's'}${names ? `: ${names}` : ''}`,
    });
  } else {
    lines.push({ kind: 'adds', text: 'Adds no skills' });
  }

  const summaries = new Map(
    (Array.isArray(details?.mcp_server_summaries) ? details.mcp_server_summaries : [])
      .map((s) => [s.name, s]),
  );
  const servers = Array.isArray(manifest.mcp_servers) ? manifest.mcp_servers : [];
  const touch = [];
  for (const server of servers) {
    const summary = summaries.get(server.name) || {};
    let line = `Adds MCP server \`${server.name}\``;
    if (summary.runs) line += ` (runs \`${summary.runs}\`)`;
    else if (summary.url) line += ` (connects to ${summary.url})`;
    lines.push({ kind: 'adds', text: line });
    if (summary.cwd) touch.push(`working folder ${summary.cwd} (for \`${server.name}\`)`);
    if (summary.url) {
      try {
        touch.push(`network host ${new URL(summary.url).host} (for \`${server.name}\`)`);
      } catch {
        touch.push(`network address ${summary.url} (for \`${server.name}\`)`);
      }
    }
    if (Array.isArray(summary.env_keys) && summary.env_keys.length > 0) {
      touch.push(`environment variables ${summary.env_keys.join(', ')} (names only — values are never shown)`);
    }
    if (Array.isArray(summary.header_keys) && summary.header_keys.length > 0) {
      touch.push(`request headers ${summary.header_keys.join(', ')} (names only — values are never shown)`);
    }
  }
  if (servers.length === 0) {
    lines.push({ kind: 'adds', text: 'Adds no MCP servers' });
  }
  if (touch.length > 0) {
    lines.push({ kind: 'touch', text: `Can touch: ${touch.join('; ')}` });
  }

  const tools = Array.isArray(manifest.tools) ? manifest.tools : [];
  if (tools.length > 0) {
    const names = tools.map((t) => t.name).filter(Boolean).join(', ');
    lines.push({
      kind: 'refused',
      text: `Declares ${tools.length} tool${tools.length === 1 ? '' : 's'}${names ? `: ${names}` : ''} — not executable. Plugins cannot add tools directly; a tool must come through a bundled MCP server, so declared tools are never installed.`,
    });
  }

  const files = manifest.files && typeof manifest.files === 'object' ? manifest.files : {};
  const fileCount = Object.keys(files).length;
  if (fileCount > 0) {
    lines.push({
      kind: 'integrity',
      text: `Package integrity: ${fileCount} file${fileCount === 1 ? '' : 's'} pinned by SHA-256 hashes in the manifest — every file is verified against them before anything is installed.`,
    });
  }

  if (details?.source) {
    lines.push({ kind: 'source', text: `Source: ${details.source}` });
  }
  return lines;
}

export default function PluginTrustCard({ details }) {
  // A payload without a manifest cannot be summarised — fall back
  // to the raw payload (the pre-Wave-5 rendering) so an approval
  // is never presented blank.
  if (!details || typeof details !== 'object' || !details.manifest) {
    return (
      <div style={{ background: 'var(--bg-base)', padding: '12px', borderRadius: '8px', fontFamily: 'var(--font-mono)', fontSize: '12px', color: 'var(--text-secondary)', overflowX: 'auto' }}>
        {JSON.stringify(details ?? {}, null, 2)}
      </div>
    );
  }
  const lines = pluginTrustLines(details);
  return (
    <div data-testid="plugin-trust-card" style={{ background: 'var(--bg-base)', padding: '12px 16px', borderRadius: '8px' }}>
      <ul style={{ margin: 0, paddingLeft: '18px', display: 'flex', flexDirection: 'column', gap: '6px' }}>
        {lines.map((line, i) => (
          <li
            key={i}
            data-trust-line={line.kind}
            style={{
              fontSize: '13px',
              lineHeight: 1.5,
              color: line.kind === 'refused' ? 'var(--danger)' : 'var(--text-secondary)',
            }}
          >
            {line.text}
          </li>
        ))}
      </ul>
    </div>
  );
}
