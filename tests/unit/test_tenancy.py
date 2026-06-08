"""Unit tests for MM-9.3 tenant identity resolution."""

from __future__ import annotations

from config import Settings


def test_key_tenant_map_combines_plain_and_tenant_keys():
    s = Settings(
        api_keys="k1,k2",
        tenant_api_keys='{"k3":"acme","k4":"globex"}',
        default_tenant="base",
    )
    assert s.key_tenant_map == {
        "k1": "base",
        "k2": "base",
        "k3": "acme",
        "k4": "globex",
    }


def test_tenant_key_overrides_plain_key_tenant():
    # A key present in both lists takes the explicit tenant mapping.
    s = Settings(api_keys="shared", tenant_api_keys='{"shared":"acme"}', default_tenant="base")
    assert s.key_tenant_map == {"shared": "acme"}


def test_key_tenant_map_failsafe_on_bad_json():
    # Malformed tenant map must not lock everyone out — fall back to plain keys.
    s = Settings(api_keys="k1", tenant_api_keys="{not valid json", default_tenant="base")
    assert s.key_tenant_map == {"k1": "base"}


def test_key_tenant_map_empty_when_no_keys():
    # No keys configured → auth disabled (dev mode).
    s = Settings(api_keys="", tenant_api_keys="")
    assert s.key_tenant_map == {}
