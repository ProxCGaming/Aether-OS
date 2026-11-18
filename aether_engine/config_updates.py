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

Wave 4 extends the speech set: ``speech_tts_speed`` (a preset name
from ``voice/tts.py``), ``speech_stt_language`` ('auto' or a code
from ``voice/stt.py``), ``speech_input_device_id`` (an opaque
browser-minted microphone id, shape-checked only) and
``dictation_mode`` ('hold' | 'toggle').
"""
from __future__ import annotations

from typing import Any

from aether_engine.memory.facade import VALID_BACKENDS
from aether_engine.voice.stt import STT_LANGUAGES, is_supported_stt_model
from aether_engine.voice.tts import TTS_SPEED_PRESETS, is_valid_kokoro_voice

# The microphone deviceId is minted by the browser and is opaque
# (often a long hash); the bound only keeps a hostile or corrupt
# write from parking an unbounded string in the config file.
MAX_INPUT_DEVICE_ID_LEN = 512

# The dictation interaction modes the composer implements.
DICTATION_MODES = ("hold", "toggle")

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
    "speech_tts_speed",
    "speech_stt_language",
    "speech_input_device_id",
    "dictation_mode",
    "calendar_enabled",
    "calendar_reminder_day_before",
    "calendar_reminder_lead_minutes",
    "eq_enabled",
    "subagents_enabled",
    "subagents_max_per_turn",
    "subagent_timeout_s",
    "subagent_token_budget",
    "subagent_cost_budget_usd",
    "model_routing",
    "voice_speculation_enabled",
    "computer_use_enabled",
    "computer_use_observe",
    "computer_use_vision_capture",
    "computer_use_control",
    "computer_use_control_ack",
    "computer_use_category_modes",
    "computer_use_app_rules",
    "computer_use_envelopes_enabled",
    "computer_use_pixel_fallback",
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


def _bounded_float(field: str, value: Any, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(field, "must be a number")
    if not low <= float(value) <= high:
        raise _fail(field, f"must be between {low} and {high}")
    return float(value)


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


def _validate_tts_speed(value: Any) -> str:
    """A speech-speed write (Wave 4): a preset NAME from
    voice/tts.py TTS_SPEED_PRESETS — the factor itself is never
    accepted from a client, so a stored value always means the
    pace the picker labelled."""
    if not isinstance(value, str) or value not in TTS_SPEED_PRESETS:
        raise _fail("speech_tts_speed",
                    f"unsupported speech speed {value!r} (expected "
                    f"one of: {', '.join(TTS_SPEED_PRESETS)})")
    return value


def _validate_stt_language(value: Any) -> str:
    """A speech-language write (Wave 4): 'auto' or an ISO code
    from voice/stt.py STT_LANGUAGES — the curated picker list,
    so the setting can never name a language the UI didn't
    offer."""
    if not isinstance(value, str) or value not in STT_LANGUAGES:
        raise _fail("speech_stt_language",
                    f"unsupported speech language {value!r} "
                    f"(expected 'auto' or one of: "
                    f"{', '.join(sorted(STT_LANGUAGES - {'auto'}))})")
    return value


def _validate_input_device_id(value: Any) -> str:
    """A microphone device write (Wave 4): the id is an opaque
    browser-minted string ('' = the system default), so the
    check is shape only — a bounded string, nothing coerced."""
    if not isinstance(value, str):
        raise _fail("speech_input_device_id",
                    "must be a microphone device id string "
                    "('' for the system default)")
    if len(value) > MAX_INPUT_DEVICE_ID_LEN:
        raise _fail("speech_input_device_id",
                    f"device id is too long "
                    f"({len(value)} > {MAX_INPUT_DEVICE_ID_LEN} chars)")
    return value


def _validate_dictation_mode(value: Any) -> str:
    """A dictation-mode write (Wave 4): 'hold' or 'toggle' —
    the two behaviours the composer implements."""
    if value not in DICTATION_MODES:
        raise _fail("dictation_mode",
                    f"unsupported dictation mode {value!r} "
                    f"(expected one of: {', '.join(DICTATION_MODES)})")
    return value


def _validate_desktop_category_modes(value: Any) -> dict:
    """A Computer Use category-modes write (S20): a mapping over
    the desktop policy's categories to its modes. Unknown
    categories/modes are refused naming the valid sets, and the
    hard-confirm categories refuse any mode but always_confirm
    WITH THE REASON (owner decision O1: close/quit and system
    actions confirm individually, always — no envelope, no
    Allow-freely; storing a looser mode for them is refused here
    so the data can never exist, on top of the policy's own
    mode_for forcing). A partial mapping is fine: the policy
    cleaner fills the rest with always_confirm."""
    from aether_engine.desktop.policy import (
        CATEGORIES,
        HARD_CONFIRM_CATEGORIES,
        MODES,
    )

    if not isinstance(value, dict):
        raise _fail("computer_use_category_modes",
                    "must be an object mapping category to mode")
    cleaned: dict[str, str] = {}
    for category, mode in value.items():
        if category not in CATEGORIES:
            raise _fail(
                "computer_use_category_modes",
                f"unknown category {category!r} (valid: "
                f"{', '.join(CATEGORIES)})")
        if mode not in MODES:
            raise _fail(
                "computer_use_category_modes",
                f"unknown mode {mode!r} for {category} (valid: "
                f"{', '.join(MODES)})")
        if category in HARD_CONFIRM_CATEGORIES and mode != "always_confirm":
            raise _fail(
                "computer_use_category_modes",
                f"{category} actions always confirm individually "
                "and cannot use envelopes or Allow freely — the "
                "mode must stay always_confirm")
        cleaned[category] = mode
    return cleaned


def _validate_desktop_app_rules(value: Any) -> list:
    """A Computer Use per-app rules write (S20): a list of
    {"app", "verdict"} entries over the closed verdict set
    (allow/deny), capped so the Settings list stays a list a
    human reviews. Malformed entries refuse the write (the
    strict contract) rather than being dropped silently."""
    from aether_engine.desktop.policy import APP_VERDICTS, clean_app_rules

    if not isinstance(value, list):
        raise _fail("computer_use_app_rules",
                    "must be a list of {app, verdict} entries")
    if len(value) > 50:
        raise _fail("computer_use_app_rules",
                    "at most 50 app rules — the list is meant to "
                    "stay reviewable")
    for entry in value:
        if not isinstance(entry, dict) \
                or not str(entry.get("app") or "").strip() \
                or entry.get("verdict") not in APP_VERDICTS:
            raise _fail(
                "computer_use_app_rules",
                "each entry needs a non-empty app and a verdict "
                f"of {', '.join(APP_VERDICTS)}")
    return clean_app_rules(value)


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

    # S13: bounded sub-agent spawning. Ranges mirror the consumer's
    # semantics in langgraph/subagents: the per-turn cap floors at 0
    # (0 blocks all spawning, like the delegation cap), the timeout
    # floors at 1 (a 0-second child could never run), and the token /
    # cost budgets floor at 0 where 0 DISABLES that budget (the
    # timeout and cap still bind) — a stored value must mean exactly
    # what the spawn seam reads.
    if "subagents_max_per_turn" in config_data:
        cleaned["subagents_max_per_turn"] = _bounded_int(
            "subagents_max_per_turn",
            config_data["subagents_max_per_turn"], 0, 100)

    if "subagent_timeout_s" in config_data:
        cleaned["subagent_timeout_s"] = _bounded_int(
            "subagent_timeout_s",
            config_data["subagent_timeout_s"], 1, 3600)

    if "subagent_token_budget" in config_data:
        cleaned["subagent_token_budget"] = _bounded_int(
            "subagent_token_budget",
            config_data["subagent_token_budget"], 0, 10_000_000)

    if "subagent_cost_budget_usd" in config_data:
        cleaned["subagent_cost_budget_usd"] = _bounded_float(
            "subagent_cost_budget_usd",
            config_data["subagent_cost_budget_usd"], 0.0, 10_000.0)

    # S14: model routing is a closed two-value mode, validated like
    # memory_backend — an unknown value is refused (naming the valid
    # set), never coerced, so the Settings toggle can only ever store
    # a mode the router actually implements.
    if "model_routing" in config_data:
        value = config_data["model_routing"]
        if not isinstance(value, str) or not value.strip():
            raise _fail("model_routing", "must be a routing mode")
        mode = value.strip().lower()
        if mode not in ("off", "auto"):
            raise _fail("model_routing",
                        f"unknown mode {value!r} (valid: off, auto)")
        cleaned["model_routing"] = mode

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

    # Wave 4 voice home: speed preset, STT language, microphone
    # device and dictation mode join the speech fields under the
    # same strict contract.
    if "speech_tts_speed" in config_data:
        cleaned["speech_tts_speed"] = _validate_tts_speed(
            config_data["speech_tts_speed"])

    if "speech_stt_language" in config_data:
        cleaned["speech_stt_language"] = _validate_stt_language(
            config_data["speech_stt_language"])

    if "speech_input_device_id" in config_data:
        cleaned["speech_input_device_id"] = _validate_input_device_id(
            config_data["speech_input_device_id"])

    if "dictation_mode" in config_data:
        cleaned["dictation_mode"] = _validate_dictation_mode(
            config_data["dictation_mode"])

    # Calendar layer C5 (14 Nov 2026): the switches are real
    # booleans — a truthy string from a hand-rolled client must be
    # refused, not coerced, or the UI would show a state the engine
    # never agreed to. Lead minutes are bounded like every other
    # count here; 0 is meaningful (it disables the lead reminder).
    # S11: eq_enabled joins the strict booleans on the same
    # rule — the Settings → Conversation switch is a real
    # boolean, refused rather than coerced when it is not.
    # S13: subagents_enabled joins them for the same reason — the
    # Orchestration switch is a real boolean.
    # S16: voice_speculation_enabled joins them too — a config
    # write for the speculation opt-in must be a real boolean,
    # refused rather than coerced when it is not.
    # S19: computer_use_enabled / computer_use_observe join
    # them — the Computer Use master and Observe switches are
    # real booleans; a truthy string would show a state the
    # engine never agreed to, and for a screen-reading feature
    # that distinction is the whole point of the switch.
    # S20: the actuation switches join them on the same rule —
    # control, its acknowledgement, envelopes and the pixel
    # fallback are all real booleans; for a feature that clicks
    # and types in the user's apps, a coerced truthy string is
    # exactly the failure this contract exists to prevent.
    for bool_field in ("calendar_enabled", "calendar_reminder_day_before",
                       "eq_enabled", "subagents_enabled",
                       "voice_speculation_enabled",
                       "computer_use_enabled", "computer_use_observe",
                       "computer_use_control", "computer_use_control_ack",
                       "computer_use_envelopes_enabled",
                       "computer_use_pixel_fallback"):
        if bool_field in config_data:
            value = config_data[bool_field]
            if not isinstance(value, bool):
                raise _fail(bool_field, "must be true or false")
            cleaned[bool_field] = value

    # S20: the desktop policy's stored half. Category modes are
    # a closed vocabulary (the policy taxonomy); an unknown
    # category or mode is refused naming the valid set. The
    # hard-confirm categories (close_or_quit, system) REFUSE any
    # mode but always_confirm, with the reason — the panel
    # disables those selects, and this is the write-path lock
    # behind it: an envelope or Allow-freely for close/quit must
    # not even exist as stored data. The per-app list is
    # shape-validated and capped; entries collapse by app (the
    # policy cleaner's rule).
    if "computer_use_category_modes" in config_data:
        cleaned["computer_use_category_modes"] = \
            _validate_desktop_category_modes(
                config_data["computer_use_category_modes"])
    if "computer_use_app_rules" in config_data:
        cleaned["computer_use_app_rules"] = _validate_desktop_app_rules(
            config_data["computer_use_app_rules"])

    # S19: the vision capture mode is a closed three-value set,
    # validated like model_routing — an unknown value is refused
    # (naming the valid set), never coerced, so the Settings
    # selector can only ever store a mode S20 will implement.
    # There is deliberately no "always_on" value: continuous
    # capture is not offered anywhere in Computer Use.
    if "computer_use_vision_capture" in config_data:
        value = config_data["computer_use_vision_capture"]
        if not isinstance(value, str) or not value.strip():
            raise _fail("computer_use_vision_capture",
                        "must be a capture mode")
        mode = value.strip().lower()
        if mode not in ("off", "ask", "on_demand"):
            raise _fail("computer_use_vision_capture",
                        f"unknown mode {value!r} "
                        "(valid: off, ask, on_demand)")
        cleaned["computer_use_vision_capture"] = mode

    if "calendar_reminder_lead_minutes" in config_data:
        cleaned["calendar_reminder_lead_minutes"] = _bounded_int(
            "calendar_reminder_lead_minutes",
            config_data["calendar_reminder_lead_minutes"], 0, 1440)

    return cleaned
