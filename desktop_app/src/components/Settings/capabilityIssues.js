// D4-9: the Capabilities roll-up's issue classifiers.
//
// The counts on the Capabilities tab derive from the SAME list
// payloads the owning tabs consume (MCP_SERVER_LIST_RESPONSE,
// PLUGIN_LIST_RESPONSE, SKILL_LIST_RESPONSE) and classify with
// the SAME state rules those tabs apply to their rows — the
// predicates below are the panels' own, kept in one place so
// the roll-up cannot drift into a second definition:
//
//   MCP     — McpPanel classifies a server 'error' when it is in
//             a failed state or carries an error (an invalid
//             config entry carries both status "invalid" and an
//             error, so it counts through the error half).
//   Plugins — the registry load error is payload-level reply
//             data (registry_load_error): one issue when present,
//             however many plugins it hides.
//   Skills  — SkillsPanel classifies a skill 'error' when its
//             row state is 'load_error' (a manifest that failed
//             to parse; the engine lists the row, never drops it).

export function mcpIssueCount(servers) {
  if (!servers || typeof servers !== 'object') return 0;
  return Object.values(servers).filter(
    (config) => config && (config.status === 'failed' || Boolean(config.error))
  ).length;
}

export function pluginIssueCount(pluginListPayload) {
  return pluginListPayload && pluginListPayload.registry_load_error ? 1 : 0;
}

export function skillIssueCount(skills) {
  if (!Array.isArray(skills)) return 0;
  return skills.filter((skill) => skill && skill.state === 'load_error').length;
}
