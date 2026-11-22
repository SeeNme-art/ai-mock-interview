"""集中配置：所有可变项从环境变量 / .env 读取，代码里只保留默认值。

设计约束（见 docs/项目设计/03-系统架构与运行机制.md）：
- 真实值只存在于 .env，代码中不得出现任何密钥字面量
- 预算类参数同样是配置，不允许硬编码
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录：src/app/config.py -> src/app -> src -> <root>
BASE_DIR: Path = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """应用配置。字段名与环境变量同名（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    # ---------- 基础 ----------
    app_name: str = "ai-mock-interview"
    env: str = "dev"
    log_level: str = "INFO"
    log_dir: Path = BASE_DIR / "logs"
    data_dir: Path = BASE_DIR / "data"

    # ---------- 可观测 ----------
    observe_db: Path = BASE_DIR / "data" / "observe.db"
    observe_ui: bool = True
    # 兼容旧环境变量，但 P0 不开放正文调试；true 会明确拒绝启动。
    observe_store_io: bool = False
    observe_flush_size: int = Field(default=50, ge=1)          # 攒够多少条写一次
    observe_flush_interval_ms: int = Field(default=200, ge=10)  # 或等多久写一次

    # ---------- 模型 ----------
    deepseek_api_key: str = ""
    llm_model_main: str = "deepseek-flash"
    llm_timeout_s: int = 30
    llm_max_retries: int = Field(default=1, ge=0, le=1)  # 供上层重试策略使用，适配层每次只发送一次
    llm_web_enabled: bool = False
    llm_web_total_nanos: int | None = Field(default=None, gt=0, le=10**15)
    llm_web_session_nanos: int | None = Field(default=None, gt=0, le=10**15)
    llm_web_turn_nanos: int | None = Field(default=None, gt=0, le=10**15)

    # ---------- 预算（P2 起启用） ----------
    max_tokens_per_session: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def metadata_only(self) -> "Settings":
        if self.observe_store_io:
            raise ValueError("P0 supports metadata-only telemetry; OBSERVE_STORE_IO must be false")
        if self.llm_web_enabled:
            limits = self.llm_web_turn_nanos, self.llm_web_session_nanos, self.llm_web_total_nanos
            if any(limit is None for limit in limits) or not limits[0] <= limits[1] <= limits[2]:
                raise ValueError("Cloud mode needs explicit turn <= session <= total budget limits")
            if not self.deepseek_api_key.strip() or self.llm_model_main != "deepseek-flash" or self.llm_timeout_s < 1:
                raise ValueError("Cloud mode needs a credential, deepseek-flash and a positive timeout")
        return self

    @property
    def is_dev(self) -> bool:
        return self.env.lower() in {"dev", "local", "test"}

    def ensure_dirs(self) -> None:
        """确保运行期需要的目录存在。"""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.observe_db.parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """单例配置，避免重复解析 .env。"""
    return Settings()
