from __future__ import annotations

import json
from collections.abc import Callable
from threading import Lock
from typing import Any

from mem0 import Memory

from app.config import Settings
from app.models import AddMemoryRequest, CommandResultRequest, SearchMemoryRequest
from app.sanitizer import sanitize_text, sanitize_value


MemoryFactory = Callable[[dict[str, Any]], Any]


class MemoryService:
    def __init__(
        self,
        settings: Settings,
        memory_factory: MemoryFactory | None = None,
    ) -> None:
        self.settings = settings
        self._memory_factory = memory_factory or Memory.from_config
        self._memory: Any | None = None
        self._init_lock = Lock()

    def _build_config(self) -> dict[str, Any]:
        self.settings.ensure_data_dirs()
        api_key = self.settings.memory_api_key.get_secret_value()

        return {
            "version": "v1.1",
            "history_db_path": str(self.settings.memory_data_dir / "history.db"),
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": self.settings.memory_collection,
                    "path": str(self.settings.memory_data_dir / "qdrant"),
                    "embedding_model_dims": self.settings.memory_embedding_dims,
                    "on_disk": True,
                },
            },
            "llm": {
                "provider": "openai",
                "config": {
                    "model": self.settings.memory_llm_model,
                    "api_key": api_key,
                    "openai_base_url": self.settings.memory_base_url,
                    "temperature": 0.0,
                    "max_tokens": 800,
                },
            },
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": self.settings.memory_embedding_model,
                    "api_key": api_key,
                    "openai_base_url": self.settings.memory_base_url,
                    "embedding_dims": self.settings.memory_embedding_dims,
                },
            },
            "custom_instructions": (
                "只保存对游戏服务端运维和协议调试有长期复用价值的事实。"
                "忽略寒暄、临时状态和无结论日志。"
                "不得保存密码、令牌、API Key、Cookie、完整连接串或私钥。"
                "事实必须保留 environment、server_id、server_version、command 等限定条件，"
                "避免把测试服经验错误应用到生产服。"
            ),
        }

    def _get_memory(self) -> Any:
        if self._memory is not None:
            return self._memory

        if not self.settings.api_key_configured:
            raise RuntimeError("MEMORY_API_KEY is not configured in the local .env file")

        with self._init_lock:
            if self._memory is None:
                self._memory = self._memory_factory(self._build_config())
        return self._memory

    def add(self, request: AddMemoryRequest) -> Any:
        memory = self._get_memory()
        user_id = request.user_id or self.settings.memory_default_user_id
        metadata = sanitize_value(request.metadata)
        text = sanitize_text(request.text)

        kwargs: dict[str, Any] = {
            "user_id": user_id,
            "agent_id": request.agent_id,
            "metadata": metadata,
        }
        if request.run_id:
            kwargs["run_id"] = request.run_id
        return memory.add(text, **kwargs)

    def search(self, request: SearchMemoryRequest) -> Any:
        memory = self._get_memory()
        filters = sanitize_value(request.filters)
        filters.setdefault(
            "user_id", request.user_id or self.settings.memory_default_user_id
        )
        filters.setdefault("agent_id", request.agent_id)
        if request.run_id:
            filters.setdefault("run_id", request.run_id)

        return memory.search(
            query=sanitize_text(request.query),
            filters=filters,
            top_k=request.limit,
        )

    def get_all(self, user_id: str | None = None) -> Any:
        memory = self._get_memory()
        return memory.get_all(
            filters={"user_id": user_id or self.settings.memory_default_user_id}
        )

    def delete(self, memory_id: str) -> Any:
        memory = self._get_memory()
        return memory.delete(memory_id=memory_id)

    def record_command_result(self, request: CommandResultRequest) -> Any:
        sanitized_arguments = sanitize_value(request.arguments)
        sanitized_message = sanitize_text(request.message or "")
        status = "成功" if request.success else "失败"

        payload = {
            "environment": request.environment,
            "server_id": request.server_id,
            "server_version": request.server_version,
            "command": request.command,
            "arguments": sanitized_arguments,
            "status": status,
            "error_code": request.error_code,
            "message": sanitized_message,
        }
        text = (
            "游戏服务端指令执行记录。请仅提炼可长期复用的指令约束、版本差异、"
            "错误原因或解决办法；如果没有可复用信息则不要创建记忆。\n"
            + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )
        metadata = {
            "kind": "command_result",
            "environment": request.environment,
            "server_id": request.server_id,
            "server_version": request.server_version or "unknown",
            "command": request.command,
            "success": request.success,
        }
        return self.add(
            AddMemoryRequest(
                text=text,
                user_id=request.user_id,
                agent_id="simple-game-client",
                run_id=request.run_id,
                metadata=metadata,
            )
        )
