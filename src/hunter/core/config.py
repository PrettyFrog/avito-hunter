from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from hunter.core.models import WatchItem
from hunter.core.egress_config import EgressRegulatorConfig


def project_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


class RequestBudget(BaseModel):
    daily_cap: int = 900
    max_per_day: int | None = None
    min_delay_seconds: float = 1.0
    max_delay_seconds: float = 2.0
    night_multiplier: float = 1.8
    night_hours: list[int] = Field(default_factory=lambda: [1, 7])
    night_enabled: bool = True
    backoff_start_seconds: int = 60
    circuit_minutes: int = 45
    jitter: float = 0.2


class TierLimitConfig(BaseModel):
    max_products: int = 3
    min_interval_minutes: int = 15
    max_alerts_per_hour: int = 10


class MultiuserConfig(BaseModel):
    invite_ttl_hours: int = 48  # CALIBRATE: invite valid for 48h
    max_active_invites: int = 10  # CALIBRATE: max pending invites for owner
    max_access_requests_per_hour: int = 5  # CALIBRATE: max access requests / hour
    max_pending_access_requests: int = 20  # CALIBRATE: max total pending access requests
    tiers: dict[str, TierLimitConfig] = Field(
        default_factory=lambda: {
            "family": TierLimitConfig(max_products=5, min_interval_minutes=10, max_alerts_per_hour=10),
            "friend": TierLimitConfig(max_products=3, min_interval_minutes=15, max_alerts_per_hour=10),
        }
    )


class ShadowFetchConfig(BaseModel):
    enabled: bool = False
    variant: str = "fetch"  # "fetch" or "light_nav"
    watch_id: str = "gem_headphones_sony"
    every_n_polls: int = 3
    max_per_day: int = 100
    max_total: int = 300
    min_gap_seconds: int = 20


class CollectConfig(BaseModel):
    search_mode: str = "light"  # "light" or "render"
    search_fallback_streak: int = 3
    search_concurrency: int = 4
    profile_count: int = 1
    global_min_gap_seconds: float = 3.5
    profile_cooldown_minutes: int = 15
    cpu_workers: int = 2
    light_variant: str = "light_nav"  # "fetch" or "light_nav"
    tab_recycle_interval: int = 200
    light_min_delay_seconds: float | None = None
    light_max_delay_seconds: float | None = None
    card_on_demand: bool = True
    mobile_card_api: bool = True


class PollPolicyConfig(BaseModel):
    day_interval_sec: float = 30.0
    night_interval_sec: float = 180.0
    day_start_hour: int = 7
    night_start_hour: int = 1
    timezone: str = "Europe/Moscow"
    max_queries_per_slot: int = 4


class PublicV2Config(BaseModel):
    enabled: bool = False
    protected_proxy_auth: bool = False
    require_telegram_relay: bool = True
    day_interval_sec: float = 30.0
    night_interval_sec: float = 180.0
    day_start_hour: int = 7
    night_start_hour: int = 1
    timezone: str = "Europe/Moscow"
    slot_capacity: int = 4
    reserve_slots: int = 2
    ip_monthly_rub: int = 100
    price_watch_enabled: bool = False
    grouping_exact_enabled: bool = True
    grouping_compatible_price_enabled: bool = False
    grouping_broad_umbrella_enabled: bool = False
    browser_fetch: bool = False
    jitter_ratio: float = 0.25
    max_jitter_sec: float = 180.0

    @field_validator("day_interval_sec", "night_interval_sec", "max_jitter_sec")
    @classmethod
    def _validate_intervals(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("Interval must be positive")
        return v

    @field_validator("day_start_hour", "night_start_hour")
    @classmethod
    def _validate_hours(cls, v: int) -> int:
        if not (0 <= v <= 23):
            raise ValueError("Hour must be between 0 and 23")
        return v

    @field_validator("slot_capacity")
    @classmethod
    def _validate_capacity(cls, v: int) -> int:
        if not (1 <= v <= 4):
            raise ValueError("Slot capacity must be between 1 and 4")
        return v

    @field_validator("reserve_slots", "ip_monthly_rub", "jitter_ratio")
    @classmethod
    def _validate_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("Value must be non-negative")
        return v

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except Exception as exc:
            raise ValueError(f"Invalid timezone: {v}") from exc
        return v

    @model_validator(mode="after")
    def _validate_grouping_flags(self) -> PublicV2Config:
        if self.grouping_broad_umbrella_enabled and not self.grouping_exact_enabled:
            raise ValueError("Cannot enable broad umbrella grouping without exact grouping")
        return self

    def to_status_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled, "day_interval_sec": self.day_interval_sec,
            "night_interval_sec": self.night_interval_sec, "day_start_hour": self.day_start_hour,
            "night_start_hour": self.night_start_hour, "timezone": self.timezone,
            "slot_capacity": self.slot_capacity, "reserve_slots": self.reserve_slots,
            "ip_monthly_rub": self.ip_monthly_rub, "price_watch_enabled": self.price_watch_enabled,
            "grouping_exact_enabled": self.grouping_exact_enabled,
            "grouping_compatible_price_enabled": self.grouping_compatible_price_enabled,
            "jitter_ratio": self.jitter_ratio, "max_jitter_sec": self.max_jitter_sec,
        }


class TelegramEgressConfig(BaseModel):
    primary_endpoint: str | None = None
    secondary_endpoint: str | None = None
    allowed_hosts: set[str] = Field(default_factory=set)
    hmac_secret: SecretStr | None = None
    timeout_seconds: float = 65.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        hide_input_in_errors=True,
        env_file=str(project_root() / ".env"),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        root = project_root()
        yaml_file = root / "config" / "settings.yaml"
        sources: list[PydanticBaseSettingsSource] = [
            init_settings,
            EnvSettingsSource(settings_cls, env_prefix="hunter_", env_nested_delimiter="__"),
            env_settings,
            dotenv_settings,
        ]
        if yaml_file.exists():
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=yaml_file))
        sources.append(file_secret_settings)
        return tuple(sources)

    timezone: str = "Europe/Moscow"
    heartbeat_hours: int = 24
    cold_start_days: int = 7
    notify_mode: str = "new"
    global_max_price: int | None = None

    telegram_bot_token: SecretStr | None = None
    # SecretStr avoids leaking credentials through Settings.__repr__/model_dump.
    channel_binding_secret: SecretStr | None = None
    vk_binding_enabled: bool = False
    vk_group_id: int | None = None
    vk_group_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    ntfy_url: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None
    smtp_to: str | None = None
    gigachat_credentials: SecretStr | None = None
    gigachat_scope: str = "GIGACHAT_API_PERS"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_text_model: str = "qwen3:8b"
    gemini_api_key_primary: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    gemini_api_key_secondary: SecretStr | None = None
    gemini_model: str = "gemini-3.1-flash-lite"
    openrouter_api_key: SecretStr | None = None
    openrouter_model: str = "google/gemini-2.0-flash-exp:free"
    openrouter_enabled: bool = False
    ollama_enabled: bool = False
    ollama_vlm_model: str = "llava:7b"
    telegram_api_base: str | None = None
    avito_base_url: str | None = None
    telegram_egress: TelegramEgressConfig = Field(default_factory=TelegramEgressConfig)
    # Optional proxy for Gemini and legacy Avito media fetches; public Telegram uses relay.
    http_proxy: str | None = Field(
        default_factory=lambda: os.environ.get("HTTP_PROXY") or "http://127.0.0.1:10808"
    )

    @property
    def gemini_keys(self) -> list[str]:
        keys: list[str] = []
        candidates = (
            self.gemini_api_key_primary,
            self.gemini_api_key,
            self.gemini_api_key_secondary,
        )
        for candidate in candidates:
            value = candidate.get_secret_value() if candidate else ""
            if value.strip() and value.strip() not in keys:
                keys.append(value.strip())
        return keys

    budget: dict[str, Any] = Field(default_factory=lambda: {"daily_cap": 900})
    request_budget: RequestBudget = Field(default_factory=RequestBudget)
    priority_intervals: dict[str, int] = Field(
        default_factory=lambda: {"high": 15, "normal": 30, "low": 60}
    )
    canary: dict[str, Any] = Field(default_factory=dict)
    round1: dict[str, Any] = Field(default_factory=dict)
    round2: dict[str, Any] = Field(default_factory=dict)
    seller: dict[str, Any] = Field(default_factory=dict)
    vision: dict[str, Any] = Field(default_factory=dict)
    browser: Any = Field(default_factory=dict)
    parser: dict[str, Any] = Field(default_factory=dict)
    extract_rules: dict[str, Any] = Field(default_factory=dict)
    shadow: dict[str, Any] = Field(
        default_factory=lambda: {"max_cards_per_day": {"model_watches": 80, "gem": 15}}
    )
    alerts: dict[str, Any] = Field(default_factory=lambda: {"max_per_hour": 6})
    gem_filter: dict[str, Any] = Field(default_factory=dict)
    coverage: dict[str, Any] = Field(default_factory=dict)
    ui: dict[str, Any] = Field(default_factory=lambda: {"host": "127.0.0.1", "port": 8765})
    multiuser: MultiuserConfig = Field(default_factory=MultiuserConfig)
    shadow_fetch: ShadowFetchConfig = Field(default_factory=ShadowFetchConfig)
    collect: CollectConfig = Field(default_factory=CollectConfig)
    poll_policy: PollPolicyConfig = Field(default_factory=PollPolicyConfig)
    public_v2: PublicV2Config = Field(default_factory=PublicV2Config)
    egress_regulator: EgressRegulatorConfig = Field(default_factory=EgressRegulatorConfig)

    @model_validator(mode="after")
    def _validate_vk_binding(self) -> Settings:
        if not self.vk_binding_enabled:
            return self
        if not self.public_v2.enabled:
            raise ValueError("VK binding listener requires public_v2.enabled")
        if self.vk_group_id is None or self.vk_group_id <= 0:
            raise ValueError("VK binding listener requires a positive vk_group_id")
        if self.vk_group_token is None or not self.vk_group_token.get_secret_value().strip():
            raise ValueError("VK binding listener requires VK_GROUP_TOKEN")
        if not self.channel_binding_secret:
            raise ValueError("VK binding listener requires CHANNEL_BINDING_SECRET")
        return self

    @field_validator("browser", mode="after")
    @classmethod
    def _clean_browser(cls, v: Any) -> dict[str, Any]:
        if not isinstance(v, dict):
            return {}
        return v

    @property
    def root(self) -> Path:
        return project_root()

    @property
    def data_dir(self) -> Path:
        env_val = os.environ.get("HUNTER_DATA_DIR")
        if env_val:
            return Path(env_val)
        return self.root / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hunter.db"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def profile_dir(self) -> Path:
        return self.data_dir / "browser_profile"

    @property
    def profiles_dir(self) -> Path:
        return self.data_dir / "profiles"

    @property
    def config_dir(self) -> Path:
        return self.root / "config"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    if settings.budget and "daily_cap" in settings.budget and not settings.request_budget.daily_cap:
        settings.request_budget.daily_cap = settings.budget["daily_cap"]
        settings.request_budget.max_per_day = settings.budget["daily_cap"]
    elif settings.request_budget and settings.request_budget.daily_cap and settings.budget:
        settings.budget["daily_cap"] = settings.request_budget.daily_cap
    return settings


def load_watchlists(config_dir: Path | None = None) -> list[WatchItem]:
    settings = get_settings()
    if config_dir is None:
        products_dir = settings.data_dir / "products"
        if products_dir.exists() and any(
            p.suffix == ".yaml" and not p.name.startswith(".") for p in products_dir.glob("*.yaml")
        ):
            from hunter.core.product import ProductStore

            store = ProductStore(products_dir)
            store.load_all()
            return store.as_watch_items()

    directory = config_dir or (settings.config_dir / "watchlists")
    items: list[WatchItem] = []
    if not directory.exists():
        return items
    for path in sorted(directory.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        if isinstance(raw, dict):
            raw = [raw]
        for row in raw:
            item = WatchItem.model_validate(row)
            urls = [u for u in item.search_urls if u and "PASTE_AVITO" not in u]
            item.search_urls = urls
            items.append(item)
    return items


def load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
