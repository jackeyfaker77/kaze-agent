"""加载 Akashic 主动配置，并兼容 Kaze 已保存的平铺参数。"""

from __future__ import annotations

import math
from dataclasses import fields
from collections.abc import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from proactive_v2.config import ProactiveConfig
from proactive_v2.presets import PRESETS, STRATEGY_PARAMS


class ProactiveConfigError(ValueError):
    """A proactive setting cannot be safely interpreted."""


def _table(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise ProactiveConfigError(f"{name} 必须是配置表")
    return value


def _number(value: object, name: str, *, integer: bool = False) -> int | float:
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError
        number = float(value)
        if not math.isfinite(number) or (integer and number != int(number)):
            raise ValueError
        return int(number) if integer else number
    except (TypeError, ValueError, OverflowError) as exc:
        kind = "整数" if integer else "有限数值"
        raise ProactiveConfigError(f"{name} 必须是{kind}") from exc


def _policy_values(value: Mapping[str, object], name: str) -> dict:
    result = {}
    for category, raw in value.items():
        table = _table(raw, f"{name}.{category}")
        if category not in PRESETS["daily"]:
            raise ProactiveConfigError(f"{name}.{category} 不受支持")
        allowed = set(PRESETS["daily"][category])
        prefix = "anyaction_" if category == "anyaction" else ""
        mapped = {
            key if key in allowed else prefix + key: val for key, val in table.items()
        }
        unknown = set(mapped) - allowed
        if unknown:
            raise ProactiveConfigError(
                f"{name}.{category} 存在未知参数: {sorted(unknown)}"
            )
        result.update(mapped)
    return result


def load_proactive_config(value: dict) -> ProactiveConfig:
    """Merge a cadence profile, overrides and session/transport settings."""
    target = _table(value.get("target", {}), "target")
    profiles = _table(value.get("profiles", {}), "profiles")
    profile = str(value.get("profile", value.get("preset", "daily")))
    if profile not in PRESETS and profile not in profiles:
        raise ProactiveConfigError(f"profile 无效: {profile}")
    merged = dict(STRATEGY_PARAMS)
    merged.update(_policy_values(PRESETS.get(profile, PRESETS["daily"]), "preset"))
    if profile in profiles:
        merged.update(
            _policy_values(
                _table(profiles[profile], f"profiles.{profile}"), f"profiles.{profile}"
            )
        )
    merged.update(
        _policy_values(_table(value.get("overrides", {}), "overrides"), "overrides")
    )
    merged.update(
        _policy_values(
            {"anyaction": _table(value.get("anyaction", {}), "anyaction")}, "proactive"
        )
    )
    for item in fields(ProactiveConfig):
        if item.name in value:
            merged[item.name] = value[item.name]
    sections = {
        "agent": {
            "model": "agent_tick_model",
            "max_steps": "agent_tick_max_steps",
            "content_limit": "agent_tick_content_limit",
            "web_fetch_max_chars": "agent_tick_web_fetch_max_chars",
            "context_prob": "agent_tick_context_prob",
            "delivery_cooldown_hours": "agent_tick_delivery_cooldown_hours",
        },
        "drift": {
            "enabled": "drift_enabled",
            "max_steps": "drift_max_steps",
            "min_interval_hours": "drift_min_interval_hours",
        },
        "feed": {"poll_interval_seconds": "feed_poller_interval_seconds"},
    }
    for section, mapping in sections.items():
        table = _table(value.get(section, {}), section)
        if set(table) - set(mapping):
            raise ProactiveConfigError(
                f"{section} 存在未知参数: {sorted(set(table) - set(mapping))}"
            )
        merged.update({mapping[key]: val for key, val in table.items()})
    merged.update(
        lifecycle=str(value.get("lifecycle", "wake")),
        profile=profile,
        default_channel=str(
            target.get("channel", value.get("default_channel", "desktop"))
        ),
        default_chat_id=str(target.get("chat_id", value.get("default_chat_id", ""))),
        session_key=str(value.get("session_key", "")),
    )
    config = ProactiveConfig(**merged)
    for item in fields(config):
        default = item.default
        raw = getattr(config, item.name)
        if isinstance(default, bool):
            if not isinstance(raw, bool):
                raise ProactiveConfigError(f"{item.name} 必须是布尔值")
        elif isinstance(default, (int, float)):
            fractional = {
                "delivery_dedupe_hours",
                "agent_tick_delivery_cooldown_hours",
                "drift_min_interval_hours",
                "context_only_min_interval_hours",
            }
            number = _number(
                raw,
                item.name,
                integer=isinstance(default, int) and item.name not in fractional,
            )
            setattr(config, item.name, number)
    _validate_ranges(config)
    return config


def _validate_ranges(config: ProactiveConfig) -> None:
    if config.lifecycle not in {"default", "wake"}:
        raise ProactiveConfigError("lifecycle 必须是 default 或 wake")
    for name in (
        "agent_tick_max_steps",
        "agent_tick_content_limit",
        "agent_tick_web_fetch_max_chars",
        "feed_poller_interval_seconds",
        "recent_chat_messages",
    ):
        if getattr(config, name) < 1:
            raise ProactiveConfigError(f"{name} 必须大于 0")
    if config.drift_max_steps < 3:
        raise ProactiveConfigError("drift_max_steps 不能小于 3")
    for name in (
        "agent_tick_delivery_cooldown_hours",
        "drift_min_interval_hours",
        "context_only_daily_max",
        "context_only_min_interval_hours",
    ):
        if getattr(config, name) < 0:
            raise ProactiveConfigError(f"{name} 不能为负数")
    if config.delivery_dedupe_hours <= 0:
        raise ProactiveConfigError("delivery_dedupe_hours 必须大于 0")
    if not 1 <= config.message_dedupe_recent_n <= 50:
        raise ProactiveConfigError("message_dedupe_recent_n 必须在 [1, 50] 范围内")
    if config.interval_seconds < 60:
        raise ProactiveConfigError("interval_seconds 不能小于 60")
    if not config.tick_interval_s0 >= config.tick_interval_s1 >= 1:
        raise ProactiveConfigError("tick_interval_s0 必须 >= tick_interval_s1 >= 1")
    for name in (
        "energy_contact_threshold",
        "score_weight_energy",
        "anyaction_probability_min",
        "anyaction_probability_max",
        "judge_send_threshold",
        "agent_tick_context_prob",
    ):
        if not 0 <= getattr(config, name) <= 1:
            raise ProactiveConfigError(f"{name} 必须在 [0, 1] 范围内")
    if not 0 <= config.tick_jitter < 1:
        raise ProactiveConfigError("tick_jitter 必须在 [0, 1) 范围内")
    if config.anyaction_probability_min > config.anyaction_probability_max:
        raise ProactiveConfigError("anyaction_probability_min 不能大于 probability_max")
    if (
        config.anyaction_daily_max_actions < 0
        or config.anyaction_min_interval_seconds < 0
    ):
        raise ProactiveConfigError("anyaction 配额与最小间隔不能为负数")
    if config.anyaction_idle_scale_minutes <= 0:
        raise ProactiveConfigError("anyaction_idle_scale_minutes 必须大于 0")
    if not 0 <= config.anyaction_reset_hour_local <= 23:
        raise ProactiveConfigError("anyaction_reset_hour_local 必须在 [0, 23] 范围内")
    try:
        ZoneInfo(config.anyaction_timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ProactiveConfigError("anyaction_timezone 必须是有效的 IANA 时区") from exc
