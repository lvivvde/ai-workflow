from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    memory_provider: str = "qwen"
    memory_api_key: SecretStr = Field(default=SecretStr(""))
    memory_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    memory_llm_model: str = "qwen3.7-flash"
    memory_embedding_model: str = "text-embedding-v4"
    memory_embedding_dims: int = 1024

    memory_host: str = "127.0.0.1"
    memory_port: int = 18765
    memory_data_dir: Path = PROJECT_ROOT / "data"
    memory_collection: str = "game_mcp_memory"
    memory_default_user_id: str = "local-user"

    @field_validator("memory_data_dir", mode="before")
    @classmethod
    def resolve_data_dir(cls, value: str | Path) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.resolve()

    @property
    def api_key_configured(self) -> bool:
        return bool(self.memory_api_key.get_secret_value().strip())

    def ensure_data_dirs(self) -> None:
        self.memory_data_dir.mkdir(parents=True, exist_ok=True)
        (self.memory_data_dir / "qdrant").mkdir(parents=True, exist_ok=True)

    def public_summary(self) -> dict[str, object]:
        return {
            "provider": self.memory_provider,
            "base_url": self.memory_base_url,
            "llm_model": self.memory_llm_model,
            "embedding_model": self.memory_embedding_model,
            "embedding_dims": self.memory_embedding_dims,
            "data_dir": str(self.memory_data_dir),
            "api_key_configured": self.api_key_configured,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
