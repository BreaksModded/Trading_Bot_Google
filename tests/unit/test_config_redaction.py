import pytest
from api.routes.config import redact_settings

def test_config_endpoint_redacts_api_secret():
    data = {"api_secret": "my_super_secret", "normal_field": 123, "nested": {"another_secret": "hidden"}}
    redacted = redact_settings(data)
    assert redacted["api_secret"] == "***"
    assert redacted["normal_field"] == 123
    assert redacted["nested"]["another_secret"] == "***"

def test_config_endpoint_redacts_telegram_token():
    data = {"telegram": {"bot_token": "8571936573:fake_token"}}
    redacted = redact_settings(data)
    assert redacted["telegram"]["bot_token"] == "***"

def test_config_endpoint_returns_safe_fields_intact():
    data = {"grid": {"num_levels": 5, "min_spacing_pct": 0.006}, "active_symbols": ["BTCUSDC"]}
    redacted = redact_settings(data)
    assert redacted["grid"]["num_levels"] == 5
    assert redacted["active_symbols"] == ["BTCUSDC"]
