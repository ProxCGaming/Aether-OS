import pytest
import asyncio
from aether_engine.config import UserConfig
from aether_common.contracts import Event, EventType

def test_user_config_fallback_fields():
    cfg = UserConfig()
    assert cfg.disable_fallbacks is False
    assert cfg.fallback_chain == []

    cfg.disable_fallbacks = True
    cfg.fallback_chain = ["openrouter", "groq"]
    
    d = cfg.to_dict()
    assert d["disable_fallbacks"] is True
    assert d["fallback_chain"] == ["openrouter", "groq"]

    cfg2 = UserConfig.from_dict(d)
    assert cfg2.disable_fallbacks is True
    assert cfg2.fallback_chain == ["openrouter", "groq"]

@pytest.mark.asyncio
async def test_app_fallback_config():
    # A simple test to verify event types are defined correctly
    assert EventType.CONFIG_GET_REQUEST == "CONFIG_GET_REQUEST"
    assert EventType.CONFIG_UPDATE_REQUEST == "CONFIG_UPDATE_REQUEST"
