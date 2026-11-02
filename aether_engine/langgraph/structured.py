"""Epic 1, E1-10: structured outputs per specialist.

A specialist may be configured with an output schema
(``user_config.agent_output_schemas[agent]`` — a JSON-Schema dict).
When one is configured, the specialist's final text answer must be a
JSON value that validates against it:

- the value is parsed and validated BEFORE the node returns, and the
  parsed value is stored in the ``structured_outputs`` state channel
  for programmatic consumers (the free-text answer stays as-is);
- a malformed answer gets ONE corrective retry carrying the exact
  validation errors;
- if the retry is still malformed, the node fails loudly: its output
  becomes an explicit validation-failure message and the channel
  records the error — free text never silently passes as "structured".

With no schema configured for an agent there is zero behaviour change.

The validator intentionally covers the JSON-Schema subset agent
outputs use — ``type`` (object/array/string/number/integer/boolean/
null), ``required``, ``properties``, ``items`` and ``enum`` — rather
than pulling in a full JSON-Schema engine.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("aether_engine.langgraph.structured")

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate_value(value: Any, schema: dict, path: str = "$") -> list[str]:
    """Validation errors for ``value`` against the supported subset."""
    if not isinstance(schema, dict):
        return []
    errors: list[str] = []
    expected = schema.get("type")
    if expected in _TYPE_CHECKS and not _TYPE_CHECKS[expected](value):
        errors.append(f"{path}: expected {expected}, got {type(value).__name__}")
        return errors  # deeper checks would only add noise
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']!r}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required property '{key}'")
        properties = schema.get("properties", {})
        for key, sub_schema in properties.items():
            if key in value:
                errors.extend(validate_value(value[key], sub_schema, f"{path}.{key}"))
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            errors.extend(validate_value(item, schema["items"], f"{path}[{i}]"))
    return errors


def parse_and_validate(text: str, schema: dict) -> tuple[Any | None, list[str]]:
    """Parse ``text`` as JSON (code fences tolerated) and validate it."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```json").removeprefix("```").strip()
        cleaned = cleaned.removesuffix("```").strip()
    try:
        value = json.loads(cleaned)
    except (ValueError, TypeError) as exc:
        return None, [f"output is not valid JSON: {exc}"]
    return value, validate_value(value, schema)


def output_schema_for(config: dict, agent: str) -> dict | None:
    """The configured output schema for ``agent``, if any."""
    user_config = (config.get("configurable", {}) or {}).get("user_config")
    schemas = getattr(user_config, "agent_output_schemas", None) or {}
    schema = schemas.get(agent)
    return schema if isinstance(schema, dict) and schema else None


async def apply_output_schema(*, agent: str, config: dict, provider: Any,
                              messages: list, content: str, node_name: str,
                              ) -> tuple[str, Any | None]:
    """Validate a specialist's final answer against its schema.

    Returns ``(content, structured_entry)``: on success the entry is
    the parsed value; after a failed correction retry the content is
    replaced by a loud failure message and the entry is
    ``{"error": ...}``; with no schema configured the entry is None
    and the content is untouched.
    """
    schema = output_schema_for(config, agent)
    if schema is None:
        return content, None
    value, errors = parse_and_validate(content, schema)
    if not errors:
        return content, value

    logger.warning("Structured output for %s failed validation: %s", agent, errors)
    correction = (
        "Your previous reply was not valid structured output: "
        + "; ".join(errors)
        + ". Reply with ONLY a JSON value that matches the required "
          "output schema — no prose, no code fences."
    )
    retry_messages = [
        *messages,
        {"role": "assistant", "content": content},
        {"role": "user", "content": correction},
    ]
    parts: list[str] = []
    async for chunk in provider.call_stream(retry_messages, tools=[], node_name=node_name):
        if chunk.text:
            parts.append(chunk.text)
        if chunk.finish_reason:
            break
    retry_content = "".join(parts)
    value, errors = parse_and_validate(retry_content, schema)
    if not errors:
        return retry_content, value

    detail = "; ".join(errors)
    logger.error("Structured output for %s failed validation after retry: %s", agent, detail)
    failure = (
        f"[Structured output validation failed for {agent}: the reply did "
        f"not match the configured output schema after one correction "
        f"retry. Errors: {detail}]"
    )
    return failure, {"error": detail}
