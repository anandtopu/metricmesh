"""Unit tests for the alert router fan-out and routed-sink reporting (MM-7.1)."""
from __future__ import annotations

import pytest

from alerting.router import AlertRouter, AlertSink


class _RecordingSink(AlertSink):
    """Sink that records what it was sent."""
    def __init__(self, name: str) -> None:
        self._name = name
        self.sent: list[dict] = []

    @property
    def name(self) -> str:
        return self._name

    def send(self, anomaly: dict) -> None:
        self.sent.append(anomaly)


class _FailingSink(AlertSink):
    """Sink that always raises."""
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def send(self, anomaly: dict) -> None:
        raise RuntimeError("boom")


def _anomaly() -> dict:
    return {"metric_name": "cpu.usage", "detector": "zscore", "score": 0.9, "value": 99.5}


class TestAlertRouter:
    def test_route_returns_all_successful_sink_names(self):
        router = AlertRouter().register(_RecordingSink("log")).register(_RecordingSink("slack"))
        routed = router.route(_anomaly())
        assert routed == ["log", "slack"]

    def test_partial_failure_returns_only_successful_sinks(self):
        # AC: routed_to reflects only the sinks that succeeded.
        router = (
            AlertRouter()
            .register(_RecordingSink("log"))
            .register(_FailingSink("slack"))
        )
        routed = router.route(_anomaly())
        assert routed == ["log"]

    def test_all_sinks_failing_raises(self):
        router = AlertRouter().register(_FailingSink("slack")).register(_FailingSink("pd"))
        with pytest.raises(RuntimeError):
            router.route(_anomaly())

    def test_each_sink_receives_the_anomaly(self):
        s1, s2 = _RecordingSink("a"), _RecordingSink("b")
        router = AlertRouter().register(s1).register(s2)
        a = _anomaly()
        router.route(a)
        assert s1.sent == [a]
        assert s2.sent == [a]


# ── Routing rules (MM-6.6) ─────────────────────────────────────────────────
import json  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import patch  # noqa: E402

from alerting.router import build_default_router, select_sink_names  # noqa: E402


def _settings(rules: str = "", slack: str = "https://slack", pd: str = "pdkey", **overrides):
    base = dict(
        slack_webhook_url=slack,
        pagerduty_routing_key=pd,
        alert_routing_rules=rules,
        # MM-6.5 sinks — all opt-in / off by default.
        teams_webhook_url="",
        generic_webhook_url="",
        generic_webhook_headers="",
        smtp_host="",
        smtp_port=587,
        smtp_username="",
        smtp_password="",
        smtp_use_tls=True,
        alert_email_from="",
        alert_email_to="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestRoutingRules:
    def test_select_first_match_wins(self):
        rules = [{"match": "db.*", "sinks": ["pagerduty"]}, {"match": "*", "sinks": ["log"]}]
        assert select_sink_names("db.query.duration_ms", rules) == ["pagerduty"]
        assert select_sink_names("cpu.usage", rules) == ["log"]

    def test_select_no_match_returns_none(self):
        rules = [{"match": "db.*", "sinks": ["pagerduty"]}]
        assert select_sink_names("cpu.usage", rules) is None

    def test_no_rules_fans_out_to_all_sinks(self):
        with patch("config.get_settings", return_value=_settings(rules="")):
            router = build_default_router("cpu.usage")
        assert {s.name for s in router._sinks} == {"log", "slack", "pagerduty"}

    def test_rule_routes_to_subset(self):
        rules = json.dumps([{"match": "db.*", "sinks": ["pagerduty", "log"]},
                            {"match": "*", "sinks": ["log"]}])
        with patch("config.get_settings", return_value=_settings(rules=rules)):
            r_db = build_default_router("db.query.duration_ms")
            r_cpu = build_default_router("cpu.usage")
        assert {s.name for s in r_db._sinks} == {"pagerduty", "log"}
        assert {s.name for s in r_cpu._sinks} == {"log"}

    def test_unknown_sink_falls_back_to_log(self):
        rules = json.dumps([{"match": "*", "sinks": ["email"]}])  # email not configured
        with patch("config.get_settings", return_value=_settings(rules=rules)):
            router = build_default_router("cpu.usage")
        assert {s.name for s in router._sinks} == {"log"}


# ── Email / Teams / generic sinks (MM-6.5) ─────────────────────────────────
from alerting.router import (  # noqa: E402
    EmailSink,
    TeamsSink,
    _parse_webhook_headers,
    available_sinks,
)


class TestExtraSinks:
    def test_available_sinks_registers_configured_optins(self):
        s = _settings(
            slack="", pd="",
            teams_webhook_url="https://teams",
            generic_webhook_url="https://hook",
            generic_webhook_headers='{"X-Token": "abc"}',
            smtp_host="smtp.example.com",
            alert_email_to="ops@example.com,oncall@example.com",
        )
        sinks = available_sinks(s)
        assert set(sinks) == {"log", "teams", "webhook", "email"}

    def test_available_sinks_only_log_when_nothing_configured(self):
        s = _settings(slack="", pd="")
        assert set(available_sinks(s)) == {"log"}

    def test_email_sink_omitted_without_recipients(self):
        # smtp_host set but no recipients → email sink must not register.
        s = _settings(slack="", pd="", smtp_host="smtp.example.com", alert_email_to="")
        assert "email" not in available_sinks(s)

    def test_new_sink_is_routable_without_changing_router(self):
        # Open/closed: a routing rule can select 'teams' purely via config.
        rules = json.dumps([{"match": "*", "sinks": ["teams"]}])
        with patch(
            "config.get_settings",
            return_value=_settings(rules=rules, slack="", pd="", teams_webhook_url="https://teams"),
        ):
            router = build_default_router("cpu.usage")
        assert {s.name for s in router._sinks} == {"teams"}

    def test_teams_sink_posts_messagecard(self):
        anomaly = {"metric_name": "cpu.usage", "value": 9.9, "score": 0.95,
                   "detector": "statistical", "method": "zscore"}
        with patch("alerting.router.httpx.Client") as MockClient:
            client = MockClient.return_value.__enter__.return_value
            TeamsSink("https://teams").send(anomaly)
            client.post.assert_called_once()
            payload = client.post.call_args.kwargs["json"]
            assert payload["@type"] == "MessageCard"
            assert payload["themeColor"] == "D7263D"   # score > 0.9 → red
            facts = {f["name"]: f["value"] for f in payload["sections"][0]["facts"]}
            assert facts["Metric"] == "cpu.usage" and facts["Score"] == "95%"

    def test_email_sink_sends_via_smtp(self):
        anomaly = {"metric_name": "mem.usage", "value": 1.0, "score": 0.8, "detector": "zscore"}
        with patch("smtplib.SMTP") as MockSMTP:
            smtp = MockSMTP.return_value.__enter__.return_value
            EmailSink(
                host="smtp.x", port=587, sender="mm@x", recipients=["a@y", "b@y"],
                username="u", password="p", use_tls=True,
            ).send(anomaly)
            smtp.starttls.assert_called_once()
            smtp.login.assert_called_once_with("u", "p")
            smtp.send_message.assert_called_once()
            sent = smtp.send_message.call_args.args[0]
            assert sent["To"] == "a@y, b@y"
            assert "mem.usage" in sent["Subject"]

    def test_email_sink_skips_login_and_tls_when_not_configured(self):
        anomaly = {"metric_name": "m", "value": 1.0, "score": 0.5, "detector": "z"}
        with patch("smtplib.SMTP") as MockSMTP:
            smtp = MockSMTP.return_value.__enter__.return_value
            EmailSink(host="smtp.x", port=25, sender="mm@x", recipients=["a@y"],
                      use_tls=False).send(anomaly)
            smtp.starttls.assert_not_called()
            smtp.login.assert_not_called()
            smtp.send_message.assert_called_once()

    def test_webhook_headers_parse_failsafe(self):
        assert _parse_webhook_headers('{"A": "1"}') == {"A": "1"}
        assert _parse_webhook_headers("") == {}
        assert _parse_webhook_headers("not json") == {}      # fail-safe
        assert _parse_webhook_headers('["a"]') == {}         # not an object
