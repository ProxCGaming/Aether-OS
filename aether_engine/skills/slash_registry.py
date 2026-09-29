from dataclasses import dataclass, field
from typing import List, Dict, Optional

@dataclass
class CommandFlag:
    name: str
    description: str

@dataclass
class SlashCommand:
    command: str
    description: str
    type: str # "agent_route", "skill_invoke", "system", "composite"
    target: str
    aliases: List[str] = field(default_factory=list)
    flags: List[CommandFlag] = field(default_factory=list)
    
    def to_dict(self):
        return {
            "command": self.command,
            "description": self.description,
            "type": self.type,
            "target": self.target,
            "aliases": self.aliases,
            "flags": [{"name": f.name, "description": f.description} for f in self.flags]
        }

BUILTIN_SLASH_COMMANDS = [
    SlashCommand("/code", "Route to Coder agent", "agent_route", "coder", ["/c", "/write"]),
    SlashCommand("/research", "Route to Researcher agent", "agent_route", "researcher", ["/r", "/search"]),
    SlashCommand("/plan", "Route to Planner agent", "agent_route", "planner", ["/p"]),
    SlashCommand("/clear", "Clear chat history", "system", "clear_chat"),
    SlashCommand("/model", "Switch model for this chat", "system", "set_model"),
    SlashCommand("/tools", "List available tools", "system", "list_tools"),
    SlashCommand("/skills", "List available skills", "system", "list_skills"),
    SlashCommand("/help", "Show all commands", "system", "show_help"),
]

class SlashRegistry:
    def __init__(self):
        self._commands: Dict[str, SlashCommand] = {}
        for cmd in BUILTIN_SLASH_COMMANDS:
            self.register(cmd)
            
    def register(self, cmd: SlashCommand):
        self._commands[cmd.command] = cmd
        for alias in cmd.aliases:
            self._commands[alias] = cmd
            
    def resolve(self, command_str: str) -> Optional[SlashCommand]:
        base_cmd = command_str.split(" ")[0].lower()
        return self._commands.get(base_cmd)
        
    def get_all(self) -> List[SlashCommand]:
        # Return unique commands
        unique_cmds = {cmd.command: cmd for cmd in self._commands.values()}
        return list(unique_cmds.values())

GLOBAL_SLASH_REGISTRY = SlashRegistry()
