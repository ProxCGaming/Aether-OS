/** Wave 5 (D4-4): the shared plugin trust card — one readable
 * summary of a prepare payload for the chat approval card and the
 * Settings panel alike. Pins: the adds lines (skills, MCP servers
 * with what they run), the can-touch line with NAMES only (a
 * secret value in the payload must never reach the render), the
 * declared-tools refusal line, the integrity pin line, and the
 * raw-payload fallback for a payload that carries no manifest. */
import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import PluginTrustCard, { pluginTrustLines } from '../PluginTrustCard';

const DETAILS = {
  source: '/home/user/plugins/weather.zip',
  manifest: {
    name: 'weather',
    version: '2.0',
    description: 'Weather tools',
    skills: [{ name: 'forecast' }, { name: 'alerts' }],
    tools: [],
    mcp_servers: [{ name: 'weather-api' }],
    files: { 'skill.md': 'a'.repeat(64), 'server.json': 'b'.repeat(64) },
  },
  mcp_server_summaries: [
    {
      name: 'weather-api',
      runs: 'npx -y weather-mcp',
      env_keys: ['WEATHER_API_KEY'],
    },
  ],
};

describe('PluginTrustCard', () => {
  test('renders the adds lines in plain language', () => {
    render(<PluginTrustCard details={DETAILS} />);
    expect(screen.getByText('Adds 2 skills: forecast, alerts')).toBeInTheDocument();
    expect(screen.getByText('Adds MCP server `weather-api` (runs `npx -y weather-mcp`)')).toBeInTheDocument();
  });

  test('the can-touch line names env vars but can never show a value', () => {
    render(<PluginTrustCard details={DETAILS} />);
    const touch = screen.getByText(/Can touch:/);
    expect(touch).toHaveTextContent('WEATHER_API_KEY');
    expect(touch).toHaveTextContent('names only');
  });

  test('the integrity line reports the manifest hash pin', () => {
    render(<PluginTrustCard details={DETAILS} />);
    expect(screen.getByText(/Package integrity: 2 files pinned by SHA-256/)).toBeInTheDocument();
    expect(screen.getByText('Source: /home/user/plugins/weather.zip')).toBeInTheDocument();
  });

  test('declared tools render as not executable', () => {
    const details = {
      ...DETAILS,
      manifest: { ...DETAILS.manifest, tools: [{ name: 'shell' }] },
    };
    render(<PluginTrustCard details={details} />);
    expect(screen.getByText(/Declares 1 tool: shell — not executable/)).toBeInTheDocument();
  });

  test('a bundle with nothing in it says so instead of rendering blank', () => {
    const lines = pluginTrustLines({
      source: 'x',
      manifest: { name: 'empty', version: '1', skills: [], tools: [], mcp_servers: [], files: {} },
    });
    expect(lines.map((l) => l.text)).toEqual(
      expect.arrayContaining(['Adds no skills', 'Adds no MCP servers']),
    );
  });

  test('a payload without a manifest falls back to the raw payload', () => {
    render(<PluginTrustCard details={{ version: '1.0' }} />);
    expect(screen.queryByTestId('plugin-trust-card')).not.toBeInTheDocument();
    expect(screen.getByText(/"version": "1.0"/)).toBeInTheDocument();
  });
});
