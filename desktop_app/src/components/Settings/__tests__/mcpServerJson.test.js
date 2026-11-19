/** server.json import conversion: registry documents become
 * Add-form fields — remotes first, then npm/PyPI packages; other
 * package types and malformed documents are refused with a
 * message, never silently mis-converted. */
import { parseServerJson, aetherServerName } from '../mcpServerJson';

describe('aetherServerName', () => {
  test('keeps a qualified registry name that fits the engine rule', () => {
    expect(aetherServerName('io.example/widgets')).toBe('io.example/widgets');
  });

  test('falls back to the last segment when the name cannot be used as-is', () => {
    expect(aetherServerName('a/b/c')).toBe('c');
    expect(aetherServerName('')).toBe('imported-server');
  });
});

describe('parseServerJson', () => {
  test('converts an npm package to an npx run with its env names', () => {
    const parsed = parseServerJson(JSON.stringify({
      name: 'io.example/widgets',
      version: '1.0.0',
      packages: [{
        registryType: 'npm', identifier: '@example/widgets-mcp', version: '1.0.0',
        transport: { type: 'stdio' },
        environmentVariables: [{ name: 'WIDGET_TOKEN', isSecret: true }],
      }],
    }));
    expect(parsed.registryName).toBe('io.example/widgets');
    expect(parsed.version).toBe('1.0.0');
    expect(parsed.name).toBe('io.example/widgets');
    expect(parsed.command).toBe('npx');
    expect(parsed.args).toBe('-y, @example/widgets-mcp@1.0.0');
    expect(parsed.envPairs).toEqual([{ key: 'WIDGET_TOKEN', value: '' }]);
    expect(parsed.url).toBe('');
  });

  test('converts a PyPI package to a uvx run and keeps declared defaults', () => {
    const parsed = parseServerJson(JSON.stringify({
      name: 'io.example/pytool',
      version: '0.3.1',
      packages: [{
        registryType: 'pypi', identifier: 'pytool-mcp', version: '0.3.1',
        environmentVariables: [{ name: 'MODE', default: 'safe' }],
      }],
    }));
    expect(parsed.command).toBe('uvx');
    expect(parsed.args).toBe('pytool-mcp@0.3.1');
    expect(parsed.envPairs).toEqual([{ key: 'MODE', value: 'safe' }]);
  });

  test('prefers a streamable-http remote over packages and carries its headers', () => {
    const parsed = parseServerJson(JSON.stringify({
      name: 'io.example/hosted',
      version: '2.0.0',
      remotes: [{
        type: 'streamable-http', url: 'https://example.test/mcp',
        headers: [{ name: 'x-api-key', value: '${KEY}' }],
      }],
      packages: [{ registryType: 'npm', identifier: 'hosted-mcp', version: '2.0.0' }],
    }));
    expect(parsed.url).toBe('https://example.test/mcp');
    expect(parsed.command).toBe('');
    expect(parsed.headerPairs).toEqual([{ key: 'x-api-key', value: '${KEY}' }]);
  });

  test('refuses package types it does not convert, naming them', () => {
    expect(() => parseServerJson(JSON.stringify({
      name: 'io.example/oci',
      version: '1.0.0',
      packages: [{ registryType: 'oci', identifier: 'example/oci:latest' }],
    }))).toThrow(/oci/);
  });

  test('refuses malformed documents with a plain message', () => {
    expect(() => parseServerJson('not json')).toThrow(/not valid JSON/);
    expect(() => parseServerJson('{"version": "1.0.0"}')).toThrow(/no registry name/);
    expect(() => parseServerJson('{"name": "io.example/empty"}')).toThrow(/no remotes and no packages/);
  });
});
