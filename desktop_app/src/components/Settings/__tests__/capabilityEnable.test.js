/** D4-8: the shared enable/disable mechanism's queue derivation —
 * one definition every capability surface reads its queued state
 * from, so a toggle taken while the engine is down shows the
 * same honest "queued" state on Tools, Skills, Plugins and MCP. */
import { ENABLE_TOGGLE_TYPES, pendingEnableChange, queuedEnableVerb } from '../capabilityEnable';

const QUEUE = [
  { type: 'SKILL_TOGGLE_REQUEST', payload: { name: 'pdf-tools', enabled: true } },
  { type: 'PLUGIN_TOGGLE_REQUEST', payload: { name: 'weather', enabled: false } },
  { type: 'MCP_SERVER_UPDATE_REQUEST', payload: { name: 'github', enabled: false } },
  { type: 'TOOL_ENABLED_SET_REQUEST', payload: { tool_name: 'read_file', enabled: false } },
  { type: 'CONFIG_UPDATE_REQUEST', payload: { config: { approval_mode: 'write' } } },
];

describe('pendingEnableChange', () => {
  test('finds each surface\'s queued toggle by its own type and name key', () => {
    expect(pendingEnableChange(QUEUE, ENABLE_TOGGLE_TYPES.skill, 'name', 'pdf-tools'))
      .toEqual(QUEUE[0]);
    expect(pendingEnableChange(QUEUE, ENABLE_TOGGLE_TYPES.plugin, 'name', 'weather'))
      .toEqual(QUEUE[1]);
    expect(pendingEnableChange(QUEUE, ENABLE_TOGGLE_TYPES.mcpServer, 'name', 'github'))
      .toEqual(QUEUE[2]);
    expect(pendingEnableChange(QUEUE, ENABLE_TOGGLE_TYPES.tool, 'tool_name', 'read_file'))
      .toEqual(QUEUE[3]);
  });

  test('a different item, type or an empty queue derives nothing', () => {
    expect(pendingEnableChange(QUEUE, ENABLE_TOGGLE_TYPES.tool, 'tool_name', 'wipe')).toBeUndefined();
    expect(pendingEnableChange(QUEUE, ENABLE_TOGGLE_TYPES.skill, 'name', 'read_file')).toBeUndefined();
    expect(pendingEnableChange([], ENABLE_TOGGLE_TYPES.tool, 'tool_name', 'read_file')).toBeUndefined();
    expect(pendingEnableChange(undefined, ENABLE_TOGGLE_TYPES.tool, 'tool_name', 'read_file')).toBeUndefined();
    expect(pendingEnableChange(null, 'TOOL_ENABLED_SET_REQUEST', 'tool_name', 'read_file')).toBeUndefined();
  });

  test('malformed queue entries are skipped, never thrown on', () => {
    const messy = [null, { type: 'TOOL_ENABLED_SET_REQUEST' }, { type: 'TOOL_ENABLED_SET_REQUEST', payload: null }, QUEUE[3]];
    expect(pendingEnableChange(messy, 'TOOL_ENABLED_SET_REQUEST', 'tool_name', 'read_file')).toEqual(QUEUE[3]);
  });
});

describe('queuedEnableVerb', () => {
  test('names the action the queued payload will apply', () => {
    expect(queuedEnableVerb(QUEUE[0])).toBe('Enable');
    expect(queuedEnableVerb(QUEUE[1])).toBe('Disable');
    expect(queuedEnableVerb(undefined)).toBe('Disable');
  });
});
