from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)


# ── Sink interface (abstract base class) ──────────────────────────────────
class AlertSink(ABC):
    """
    Abstract sink. Concrete implementations must override send().
    Python skill: ABC for interface definition — unlike Protocol, ABC
    supports shared implementation via concrete methods on the base.
    """
    @abstractmethod
    def send(self, anomaly: dict[str, Any]) -> None: ...

    @property
    @abstractmethod
    def name(self) -> str: ...


# ── Concrete sinks ────────────────────────────────────────────────────────
class SlackSink(AlertSink):
    """Post an alert to a Slack Incoming Webhook URL."""
    name = "slack"

    def __init__(self, webhook_url: str) -> None:
        self._url = webhook_url

    def send(self, anomaly: dict[str, Any]) -> None:
        score_pct = round(float(anomaly.get("score", 0)) * 100)
        payload = {
            "text": (
                f":rotating_light: *MetricMesh Anomaly*\n"
                f"*Metric*: `{anomaly.get('metric_name', '?')}`\n"
                f"*Value*: `{anomaly.get('value', '?'):.4f}` — "
                f"score *{score_pct}%*\n"
                f"*Detector*: `{anomaly.get('detector', '?')}`\n"
                f"*Method*: `{anomaly.get('method', '-')}`\n"
                f"*Time*: {anomaly.get('timestamp', '?')}"
            )
        }
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(self._url, json=payload)
            resp.raise_for_status()
        log.info("slack.sent", metric=anomaly.get("metric_name"))


class PagerDutySink(AlertSink):
    """Create a PagerDuty incident via Events API v2."""
    name = "pagerduty"

    def __init__(self, routing_key: str) -> None:
        self._routing_key = routing_key

    def send(self, anomaly: dict[str, Any]) -> None:
        payload = {
            "routing_key": self._routing_key,
            "event_action": "trigger",
            "payload": {
                "summary": (
                    f"Anomaly in {anomaly.get('metric_name', '?')} "
                    f"(score {round(float(anomaly.get('score', 0)) * 100)}%)"
                ),
                "severity": "critical" if float(anomaly.get("score", 0)) > 0.9 else "warning",
                "source": "metricmesh",
                "custom_details": anomaly,
            },
        }
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(
                "https://events.pagerduty.com/v2/enqueue", json=payload
            )
            resp.raise_for_status()
        log.info("pagerduty.sent", metric=anomaly.get("metric_name"))


class WebhookSink(AlertSink):
    """POST the full anomaly dict as JSON to an arbitrary URL."""
    name = "webhook"

    def __init__(self, url: str, headers: dict[str, str] | None = None) -> None:
        self._url = url
        self._headers = headers or {}

    def send(self, anomaly: dict[str, Any]) -> None:
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(self._url, json=anomaly, headers=self._headers)
            resp.raise_for_status()
        log.info("webhook.sent", url=self._url, metric=anomaly.get("metric_name"))


class TeamsSink(AlertSink):
    """Post an alert to a Microsoft Teams Incoming Webhook as a MessageCard."""
    name = "teams"

    def __init__(self, webhook_url: str) -> None:
        self._url = webhook_url

    def send(self, anomaly: dict[str, Any]) -> None:
        score = float(anomaly.get("score", 0))
        card = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            # Red for high-severity, amber otherwise — mirrors PagerDuty severity.
            "themeColor": "D7263D" if score > 0.9 else "F2A900",
            "summary": f"MetricMesh anomaly in {anomaly.get('metric_name', '?')}",
            "title": f"🚨 MetricMesh anomaly: {anomaly.get('metric_name', '?')}",
            "sections": [
                {
                    "facts": [
                        {"name": "Metric", "value": str(anomaly.get("metric_name", "?"))},
                        {"name": "Value", "value": f"{float(anomaly.get('value', 0)):.4f}"},
                        {"name": "Score", "value": f"{round(score * 100)}%"},
                        {"name": "Detector", "value": str(anomaly.get("detector", "?"))},
                        {"name": "Method", "value": str(anomaly.get("method", "-"))},
                        {"name": "Time", "value": str(anomaly.get("timestamp", "?"))},
                    ],
                }
            ],
        }
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(self._url, json=card)
            resp.raise_for_status()
        log.info("teams.sent", metric=anomaly.get("metric_name"))


class EmailSink(AlertSink):
    """Send an alert email over SMTP (stdlib smtplib, no third-party deps)."""
    name = "email"

    def __init__(
        self,
        host: str,
        port: int,
        sender: str,
        recipients: list[str],
        username: str = "",
        password: str = "",
        use_tls: bool = True,
    ) -> None:
        self._host = host
        self._port = port
        self._sender = sender
        self._recipients = recipients
        self._username = username
        self._password = password
        self._use_tls = use_tls

    def send(self, anomaly: dict[str, Any]) -> None:
        import smtplib
        from email.message import EmailMessage

        score_pct = round(float(anomaly.get("score", 0)) * 100)
        metric = anomaly.get("metric_name", "?")
        msg = EmailMessage()
        msg["Subject"] = f"[MetricMesh] Anomaly in {metric} (score {score_pct}%)"
        msg["From"] = self._sender
        msg["To"] = ", ".join(self._recipients)
        msg.set_content(
            "MetricMesh detected an anomaly.\n\n"
            f"Metric:   {metric}\n"
            f"Value:    {float(anomaly.get('value', 0)):.4f}\n"
            f"Score:    {score_pct}%\n"
            f"Detector: {anomaly.get('detector', '?')}\n"
            f"Method:   {anomaly.get('method', '-')}\n"
            f"Time:     {anomaly.get('timestamp', '?')}\n"
        )
        with smtplib.SMTP(self._host, self._port, timeout=10) as smtp:
            if self._use_tls:
                smtp.starttls()
            if self._username:
                smtp.login(self._username, self._password)
            smtp.send_message(msg)
        log.info("email.sent", metric=metric, recipients=len(self._recipients))


class LogSink(AlertSink):
    """Fallback sink: logs the anomaly via structlog. Always available."""
    name = "log"

    def send(self, anomaly: dict[str, Any]) -> None:
        log.warning(
            "anomaly.detected",
            metric=anomaly.get("metric_name"),
            value=anomaly.get("value"),
            score=anomaly.get("score"),
            detector=anomaly.get("detector"),
        )


# ── Router ────────────────────────────────────────────────────────────────
class AlertRouter:
    """
    Fan-out router: sends each anomaly to every registered sink.
    Partial failures are collected and re-raised as a single RuntimeError
    so the Celery task can retry the full routing attempt.

    Python skill: fluent API (method chaining via return self),
    open/closed principle — adding a new sink requires zero changes here.
    """
    def __init__(self) -> None:
        self._sinks: list[AlertSink] = []

    def register(self, sink: AlertSink) -> AlertRouter:
        self._sinks.append(sink)
        return self

    def route(self, anomaly: dict[str, Any]) -> list[str]:
        """Send to every sink; return the names of the sinks that succeeded.

        Raises RuntimeError only if *all* sinks fail (so the Celery task retries
        a fully-undelivered alert). Partial failures are logged, not raised.
        """
        errors: list[tuple[str, str]] = []
        routed: list[str] = []
        for sink in self._sinks:
            try:
                sink.send(anomaly)
                routed.append(sink.name)
            except Exception as exc:
                log.error("sink.error", sink=sink.name, error=str(exc))
                errors.append((sink.name, str(exc)))

        if errors and len(errors) == len(self._sinks):
            raise RuntimeError(f"All sinks failed: {errors}")
        if errors:
            log.warning("sink.partial_failure", failures=errors)
        return routed


def _parse_webhook_headers(raw: str) -> dict[str, str]:
    """Parse the optional generic-webhook headers JSON. Fail-safe to no headers
    so a typo never breaks alert delivery."""
    if not raw.strip():
        return {}
    import json

    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
        log.error("webhook_headers.not_an_object")
    except (ValueError, TypeError) as exc:
        log.error("webhook_headers.invalid", error=str(exc))
    return {}


def available_sinks(settings: Any) -> dict[str, AlertSink]:
    """Build the registry of configured sinks, keyed by name. LogSink is always
    present as a fallback; every other sink is opt-in via config (MM-6.5).

    Adding a sink here (or any class implementing ``AlertSink``) requires **no**
    change to ``AlertRouter`` — the router fans out over whatever it's given
    (open/closed principle)."""
    sinks: dict[str, AlertSink] = {"log": LogSink()}
    if settings.slack_webhook_url:
        sinks["slack"] = SlackSink(settings.slack_webhook_url)
    if settings.pagerduty_routing_key:
        sinks["pagerduty"] = PagerDutySink(settings.pagerduty_routing_key)
    if settings.teams_webhook_url:
        sinks["teams"] = TeamsSink(settings.teams_webhook_url)
    if settings.generic_webhook_url:
        sinks["webhook"] = WebhookSink(
            settings.generic_webhook_url,
            _parse_webhook_headers(settings.generic_webhook_headers),
        )
    if settings.smtp_host and settings.alert_email_to:
        recipients = [r.strip() for r in settings.alert_email_to.split(",") if r.strip()]
        sinks["email"] = EmailSink(
            host=settings.smtp_host,
            port=settings.smtp_port,
            sender=(settings.alert_email_from or settings.smtp_username or "metricmesh@localhost"),
            recipients=recipients,
            username=settings.smtp_username,
            password=settings.smtp_password,
            use_tls=settings.smtp_use_tls,
        )
    return sinks


def select_sink_names(metric_name: str, rules: list[dict[str, Any]]) -> list[str] | None:
    """
    Return the sink names for the first rule whose ``match`` glob matches the
    metric name (MM-6.6). Returns None when no rule matches, signalling the
    caller to apply its default (fan-out to all configured sinks).
    """
    import fnmatch

    for rule in rules:
        pattern = rule.get("match", "*")
        if fnmatch.fnmatch(metric_name, pattern):
            return list(rule.get("sinks", []))
    return None


def build_default_router(metric_name: str | None = None) -> AlertRouter:
    """
    Instantiate the router from settings.

    With no routing rules configured, fans out to every configured sink (the
    original behaviour). With ``alert_routing_rules`` set and a ``metric_name``
    given, routes only to the sinks selected by the first matching rule. Unknown
    sink names are skipped, and LogSink is used as a fallback if the resolved set
    is empty so anomalies are never silently dropped.
    """
    import json

    from config import get_settings

    settings = get_settings()
    registry = available_sinks(settings)

    rules: list[dict[str, Any]] = []
    if settings.alert_routing_rules.strip():
        try:
            rules = json.loads(settings.alert_routing_rules)
        except (ValueError, TypeError) as exc:
            log.error("routing_rules.invalid", error=str(exc))

    router = AlertRouter()

    if rules and metric_name is not None:
        names = select_sink_names(metric_name, rules)
        chosen = [registry[n] for n in (names or []) if n in registry]
        if not chosen:
            chosen = [registry["log"]]
    else:
        chosen = list(registry.values())

    for sink in chosen:
        router.register(sink)
    return router
