// Importing an MCP registry entry (server.json) into the Add form.
//
// The official MCP Registry publishes one server.json document per
// server: name, version, and either hosted `remotes` or installable
// `packages`. This converts such a document into Add-form fields —
// it never saves anything itself. The form stays the single path
// to the trust card and the engine's validator, exactly as the
// registry benchmark prescribes: discovery without a store, and
// `validate_mcp_server_config` remains the gate for whatever the
// import produced.
//
// Conversion choices, deliberately narrow:
// * a streamable-http remote wins over packages when both exist
//   (it is the entry's own hosted endpoint — no local install
//   assumptions); SSE remotes are used only if nothing better
//   exists;
// * npm packages become `npx` runs and PyPI packages `uvx` runs
//   (the package's runtimeHint overrides the default runtime);
// * other package types (oci, nuget, mcpb) are refused with a
//   message naming them — the user adds those servers manually
//   rather than getting a silently wrong conversion.

// The engine's server-name rule (mcp/registry.py): 1-100 chars of
// letters, digits, '_', '.', '-', optionally qualified '<ns>/<name>'.
const AETHER_NAME_RE = /^[A-Za-z0-9_.-]+(\/[A-Za-z0-9_.-]+)?$/;

export function aetherServerName(registryName) {
  if (typeof registryName === 'string'
      && registryName.length <= 100
      && AETHER_NAME_RE.test(registryName)) {
    return registryName;
  }
  const tail = String(registryName || '').split('/').filter(Boolean).pop();
  return tail || 'imported-server';
}

// server.json argument lists mix plain strings with typed entries
// ({type: 'positional'|'named', name, value}). Flatten to the argv
// strings the form's comma-separated args field represents.
function argumentValues(entries) {
  const out = [];
  for (const entry of entries || []) {
    if (typeof entry === 'string') {
      out.push(entry);
    } else if (entry && typeof entry === 'object') {
      if (entry.type === 'named' && entry.name) {
        out.push(entry.name);
        if (entry.value) out.push(String(entry.value));
      } else if (entry.value) {
        out.push(String(entry.value));
      }
    }
  }
  return out;
}

function envPairs(pkg) {
  return (pkg.environmentVariables || [])
    .filter((v) => v && v.name)
    .map((v) => ({ key: v.name, value: v.default || '' }));
}

export function parseServerJson(text) {
  let doc;
  try {
    doc = JSON.parse(text);
  } catch {
    throw new Error('That is not valid JSON — paste the server.json document for one registry entry.');
  }
  if (!doc || typeof doc !== 'object' || typeof doc.name !== 'string' || !doc.name) {
    throw new Error('That does not look like a server.json document — it has no registry name.');
  }
  const registryName = doc.name;
  const version = typeof doc.version === 'string' ? doc.version : '';
  const base = {
    registryName,
    version,
    name: aetherServerName(registryName),
    command: '', args: '', url: '',
    headerPairs: [{ key: '', value: '' }],
    envPairs: [{ key: '', value: '' }],
  };

  const remotes = Array.isArray(doc.remotes) ? doc.remotes : [];
  const remote = remotes.find((r) => r && r.type === 'streamable-http' && r.url)
    || remotes.find((r) => r && r.url);
  if (remote) {
    const headerPairs = (remote.headers || [])
      .filter((h) => h && h.name)
      .map((h) => ({ key: h.name, value: typeof h.value === 'string' ? h.value : '' }));
    return {
      ...base,
      url: remote.url,
      headerPairs: headerPairs.length > 0 ? headerPairs : base.headerPairs,
    };
  }

  const packages = Array.isArray(doc.packages) ? doc.packages : [];
  const pkg = packages.find((p) => p && (p.registryType === 'npm' || p.registryType === 'pypi'));
  if (pkg) {
    const runtime = pkg.runtimeHint || (pkg.registryType === 'npm' ? 'npx' : 'uvx');
    const runtimeArgs = argumentValues(pkg.runtimeArguments);
    if (runtime === 'npx' && !runtimeArgs.includes('-y')) {
      // A stdio server Aether launches must install non-interactively.
      runtimeArgs.unshift('-y');
    }
    const ref = pkg.version ? `${pkg.identifier}@${pkg.version}` : pkg.identifier;
    const args = [...runtimeArgs, ref, ...argumentValues(pkg.packageArguments)];
    const pairs = envPairs(pkg);
    return {
      ...base,
      command: runtime,
      args: args.join(', '),
      envPairs: pairs.length > 0 ? pairs : base.envPairs,
    };
  }

  if (packages.length > 0) {
    const types = [...new Set(packages.map((p) => p && p.registryType).filter(Boolean))];
    throw new Error(
      `This entry offers only ${types.join(', ') || 'unknown'} package(s), which the import does not convert — add the server manually instead.`
    );
  }
  throw new Error('This entry has no remotes and no packages to import — add the server manually instead.');
}
