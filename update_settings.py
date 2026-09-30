import sys

file_path = 'e:/AI projects/AETHER/desktop_app/src/SettingsPanel.jsx'
with open(file_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
skip = False
for i, line in enumerate(lines):
    # Imports
    if i == 4 and not skip:
        new_lines.append(line)
        new_lines.append("import ProviderSettings from './components/Settings/ProviderSettings';\n")
        new_lines.append("import MemoryManagement from './components/Settings/MemoryManagement';\n")
        new_lines.append("import DiagnosticsPanel from './components/Settings/DiagnosticsPanel';\n")
        continue

    # Providers
    if line.strip() == "{activeMenu === 'Providers' && (":
        new_lines.append(line)
        new_lines.append("          <ProviderSettings providers={providers} />\n")
        skip = True
        continue
    if skip and "activeMenu === 'Models'" in line:
        skip = False
        new_lines.append("        )}\n\n")

    # Agents, MCP, Plugins
    if not skip and (line.strip() == "{activeMenu === 'Agents' && (" or line.strip() == "{ activeMenu === 'MCP' && (" or line.strip() == "{activeMenu === 'Plugins' && ("):
        active_menu_name = line.split("===")[1].split("'")[1]
        new_lines.append(f"        {{activeMenu === '{active_menu_name}' && (\n")
        new_lines.append(f"          <DiagnosticsPanel activeMenu='{active_menu_name}' />\n")
        skip = True
        continue
    
    # Memory
    if not skip and line.strip() == "{activeMenu === 'Memory' && (":
        new_lines.append(line)
        new_lines.append("          <MemoryManagement />\n")
        skip = True
        continue
        
    if skip and ("activeMenu === 'Capabilities'" in line or "activeMenu === 'Plugins'" in line or "activeMenu === 'Skills'" in line or "activeMenu === 'Memory'" in line or "activeMenu === 'Advanced'" in line):
        skip = False
        new_lines.append("        )}\n\n")

    if not skip:
        new_lines.append(line)

with open(file_path, 'w', encoding='utf-8') as f:
    f.writelines(new_lines)

print("SettingsPanel.jsx updated successfully!")
