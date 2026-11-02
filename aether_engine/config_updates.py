"""Strict validators for the Settings-writable config fields (E6-F05).

The config-file loader (``UserConfig.from_dict``) is deliberately
tolerant: a hand-edited file with a bad value falls back to defaults
rather than bricking startup. The Settings write path is the opposite
contract — a value the user just typed must be applied exactly, or
refused with an error the UI can show. Silently dropping or coercing
a Settings write is how a control becomes a placebo.

This module owns the fields handed over by Epics 1 and 2 whose
Settings surfaces were never built (E6-F05): supervisor spawn policy,
prewalk, agent output schemas, compaction thresholds and path rules.
Each validator returns the value to store, or raises ``ValueError``
with a message that names the field — the CONFIG_UPDATE handler
turns that into ``{success: False, error: ...}`` and applies nothing.

Ranges are chosen so a stored value means what the consumer reads:
the compaction reader (``compaction.compaction_config_from``) treats
0 as "unset" and substitutes the default, so these writers floor at
1 — a Settings write of 0 would otherwise display 0 while the engine
quietly ran 40/24000/12.

E4-F12 adds ``memory_backend`` under the same contract: the
CONFIG_UPDATE handler's fixed field set omitted it, so a write of
the backend was acknowledged ``success: True`` while the live
config, the saved file and the facade's backend all stayed on the
old value. It is validated against the facade's ``VALID_BACKENDS``
vocabulary and normalised the way the config loader normalises it
(strip / lower), so a stored value is exactly the name the facade
will resolve — per request, so a valid switch takes effect without
a restart.

Model hub H3 adds the three speech fields under the same contract:
``speech_stt_model`` (dictation + voice mode), ``transcription_stt_model``
(the media analyser's own setting, grill P6(c)) and
``speech_tts_voice``. The STT fields are validated against the
voice layer's vocabulary — a faster-whisper size name, or an HF
repo id the hub labeller (H1) marks in-app for speech-to-text —
because faster-whisper accepts ANY string as a repo id and an
unvalidated typo would become a surprise multi-GB download at
first voice use. The voice is validated against the Kokoro
voice-id pattern. Both checks are offline and exact: no coercion,
no network probe.
"""
from __future__ import annotations

from typing import Any

from aether_engine.memory.facade import VALID_BACKENDS
from aether_engine.voice.stt import is_supported_stt_model
from aether_engine.voice.tts import is_valid_kokoro_voice

HANDED_OFF_FIELDS = (
    "supervisor_spawn_allowlist",
    "supervisor_delegation_cap",
    "prewalk_agents",
    "agent_output_schemas",
    "compaction_trigger_messages",
    "compaction_trigger_chars",
    "compaction_keep_recent",
    "path_rules",
    "memory_backend",
    "speech_stt_model",
    "transcription_stt_model",
    "speech_tts_voice",
)

# The verdict vocabulary of tools/policy.py's _VERDICT_MAP and the
# ToolRisk values a rule may scope to.
_PATH_RULE_VERDICTS = ("Always Allow", "Require Approval", "Deny")
_PATH_RULE_RISKS = ("read_only", "write", "execute", "destructive")
_PATH_RULE_KEYS = {"prefix", "verdict", "tools", "risks"}

# The JSON-Schema subset langgraph/structured.py validates against.
_SCHEMA_TYPES = ("object", "array", "string", "number", "integer",
                 "boolean", "null")


def _fail(field: str, problem: str) -> ValueError:
    return ValueError(f"{field}: {problem}")


def _is_int(value: Any) -> bool:
    # bool is an int subclass; a checkbox leaking True is not a count.
    return isinstance(value, int) and not isinstance(value, bool)


def _str_list(field: str, value: Any) -> list[str]:
    if not isinstance(value, list):
        raise _fail(field, "must be a list of names")
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise _fail(field, "every entry must be a non-empty string")
    return list(value)


def _bounded_int(field: str, value: Any, low: int, high: int) -> int:
    if not _is_int(value):
        raise _fail(field, "must be an integer")
    if not low <= value <= high:
        raise _fail(field, f"must be between {low} and {high}")
    return value


def _validate_schema(field: str, agent: str, schema: Any) -> dict:
    """One agent's output schema, checked against the consumer subset.

    ``langgraph.structured.output_schema_for`` requires a dict and
    its validator understands ``type`` / ``required`` / ``properties``
    / ``items`` / ``enum`` — a write outside that shape would be
    stored yet never bind, so it is refused here instead.
    """
    label = f"{field}[{agent!r}]"
    if not isinstance(schema, dict):
        raise _fail(label, "must be a JSON-Schema object")
    type_ = schema.get("type")
    if type_ is not None and type_ not in _SCHEMA_TYPES:
        raise _fail(label, f"unsupported schema type {type_!r} "
                           f"(supported: {', '.join(_SCHEMA_TYPES)})")
    required = schema.get("required")
    if required is not None and (
            not isinstance(required, list)
            or any(not isinstance(r, str) for r in required)):
        raise _fail(label, "'required' must be a list of property names")
    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            raise _fail(label, "'properties' must be an object of schemas")
        for prop, sub in properties.items():
            if not isinstance(sub, dict):
                raise _fail(label, f"property {prop!r} must be a schema object")
    items = schema.get("items")
    if items is not None and not isinstance(items, dict):
        raise _fail(label, "'items' must be a schema object")
    enum = schema.get("enum")
    if enum is not None and not isinstance(enum, list):
        raise _fail(label, "'enum' must be a list of allowed values")
    return dict(schema)


def _validate_path_rule(index: int, rule: Any) -> dict:
    field = f"path_rules[{index}]"
    if not isinstance(rule, dict):
        raise _fail(field, "each rule must be an object")
    unknown = sorted(set(rule) - _PATH_RULE_KEYS)
    if unknown:
        # The consumer ignores unknown keys — storing one would
        # silently drop part of what the user wrote.
        raise _fail(field, f"unknown rule keys: {', '.join(unknown)} "
                           f"(allowed: {', '.join(sorted(_PATH_RULE_KEYS))})")
    prefix = rule.get("prefix")
    if not isinstance(prefix, str) or not prefix.strip():
        raise _fail(field, "'prefix' must be a non-empty path string")
    verdict = rule.get("verdict")
    if verdict not in _PATH_RULE_VERDICTS:
        raise _fail(field, f"'verdict' must be one of "
                           f"{', '.join(_PATH_RULE_VERDICTS)}")
    cleaned: dict[str, Any] = {"prefix": prefix, "verdict": verdict}
    if "tools" in rule:
        cleaned["tools"] = _str_list(f"{field}.tools", rule["tools"])
    if "risks" in rule:
        risks = rule["risks"]
        if not isinstance(risks, list) or any(
                r not in _PATH_RULE_RISKS for r in risks):
            raise _fail(f"{field}.risks",
                        f"must be a list drawn from {', '.join(_PATH_RULE_RISKS)}")
        cleaned["risks"] = list(risks)
    return cleaned


def _validate_stt_model(field: str, value: Any) -> str:
    """A speech STT model write (H3): a faster-whisper size name or
    an in-app faster-whisper HF repo id — see the module docstring
    for why anything else must be refused, never coerced."""
    if not isinstance(value, str) or not value.strip():
        raise _fail(field, "must be a faster-whisper model name or "
                           "a Hugging Face repo id")
    name = value.strip()
    if not is_supported_stt_model(name):
        raise _fail(field, f"unsupported speech model {value!r} "
                           f"(use a faster-whisper size such as 'small', "
                           f"'medium' or 'large-v3', or a Hugging Face "
                           f"faster-whisper-* repo id)")
    return name


def _validate_tts_voice(value: Any) -> str:
    """A Kokoro voice write (H3): the voice-id pattern from
    voice/tts.py, e.g. 'af_heart' — anything else names no voice
    Kokoro can load."""
    if not isinstance(value, str) or not value.strip():
        raise _fail("speech_tts_voice", "must be a Kokoro voice id")
    voice = value.strip()
    if not is_valid_kokoro_voice(voice):
        raise _fail("speech_tts_voice",
                    f"unknown Kokoro voice {value!r} (voice ids look "
                    f"like 'af_heart': a language/gender prefix and "
                    f"a lowercase name)")
    return voice


def validate_handed_off_updates(config_data: dict) -> dict[str, Any]:
    """Validate the handed-off fields present in a CONFIG_UPDATE.

    Returns ``{field: value-to-store}`` for the handed-off fields in
    ``config_data`` (absent fields are not the caller's business).
    Raises ``ValueError`` naming the offending field on the first
    malformed value; the caller applies nothing in that case.
    """
    cleaned: dict[str, Any] = {}

    if "supervisor_spawn_allowlist" in config_data:
        value = config_data["supervisor_spawn_allowlist"]
        # None is meaningful: it lifts the restriction entirely.
        cleaned["supervisor_spawn_allowlist"] = (
            None if value is None
            else _str_list("supervisor_spawn_allowlist", value))

    if "supervisor_delegation_cap" in config_data:
        cleaned["supervisor_delegation_cap"] = _bounded_int(
            "supervisor_delegation_cap",
            config_data["supervisor_delegation_cap"], 0, 1000)

    if "prewalk_agents" in config_data:
        cleaned["prewalk_agents"] = _str_list(
            "prewalk_agents", config_data["prewalk_agents"])

    if "agent_output_schemas" in config_data:
        value = config_data["agent_output_schemas"]
        if not isinstance(value, dict):
            raise _fail("agent_output_schemas",
                        "must be an object mapping agent names to schemas")
        schemas: dict[str, Any] = {}
        for agent, schema in value.items():
            if not isinstance(agent, str) or not agent.strip():
                raise _fail("agent_output_schemas",
                            "agent names must be non-empty strings")
            schemas[agent] = _validate_schema(
                "agent_output_schemas", agent, schema)
        cleaned["agent_output_schemas"] = schemas

    if "compaction_trigger_messages" in config_data:
        cleaned["compaction_trigger_messages"] = _bounded_int(
            "compaction_trigger_messages",
            config_data["compaction_trigger_messages"], 1, 100_000)

    if "compaction_trigger_chars" in config_data:
        cleaned["compaction_trigger_chars"] = _bounded_int(
            "compaction_trigger_chars",
            config_data["compaction_trigger_chars"], 1, 100_000_000)

    if "compaction_keep_recent" in config_data:
        cleaned["compaction_keep_recent"] = _bounded_int(
            "compaction_keep_recent",
            config_data["compaction_keep_recent"], 1, 100_000)

    if "path_rules" in config_data:
        value = config_data["path_rules"]
        if not isinstance(value, list):
            raise _fail("path_rules", "must be a list of rule objects")
        cleaned["path_rules"] = [
            _validate_path_rule(i, rule) for i, rule in enumerate(value)]

    if "memory_backend" in config_data:
        value = config_data["memory_backend"]
        if not isinstance(value, str) or not value.strip():
            raise _fail("memory_backend", "must be a backend name")
        name = value.strip().lower()
        if name not in VALID_BACKENDS:
            raise _fail("memory_backend",
                        f"unknown backend {value!r} "
                        f"(valid: {', '.join(VALID_BACKENDS)})")
        cleaned["memory_backend"] = name

    if "speech_stt_model" in config_data:
        cleaned["speech_stt_model"] = _validate_stt_model(
            "speech_stt_model", config_data["speech_stt_model"])

    if "transcription_stt_model" in config_data:
        cleaned["transcription_stt_model"] = _validate_stt_model(
            "transcription_stt_model",
            config_data["transcription_stt_model"])

    if "speech_tts_voice" in config_data:
        cleaned["speech_tts_voice"] = _validate_tts_voice(
            config_data["speech_tts_voice"])

    return cleaned
