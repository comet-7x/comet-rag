import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .schemas import APPConfig

#: 配置文件路径的环境变量。**这是"配置路径可指定"的落点**：
#: 此前只会从 cwd 读 `config.yaml`，于是进程必须在项目根目录启动，
#: 而 uvicorn 的 `--reload` 会另起子进程 —— 只有环境变量传得过去。
ENV_CONFIG_PATH = "COMET_RAG_CONFIG"
DEFAULT_CONFIG_PATH = "config.yaml"


def resolve_config_path(path: str | None = None) -> str:
    """显式参数 > 环境变量 > cwd 下的 config.yaml。"""
    return path or os.environ.get(ENV_CONFIG_PATH) or DEFAULT_CONFIG_PATH


@dataclass
class BaseConfigLoader(ABC):
    """
    抽象配置加载基类
    """

    @classmethod
    @abstractmethod
    def load(cls, *args, **kwargs) -> Any:
        pass


@dataclass
class YamlConfigLoader(BaseConfigLoader):
    yaml_path: str = "config.yaml"

    @classmethod
    def load(cls, path: str | None = None) -> dict:
        yaml_path = resolve_config_path(path)

        if not Path(yaml_path).exists():
            return {}

        with open(yaml_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    @classmethod
    def get_config(cls, path: str | None = None) -> APPConfig:
        return APPConfig.model_validate(cls.load(path))


@lru_cache
def get_config(path: str | None = None, type: str = "yaml") -> APPConfig:
    """读配置。**进程级缓存**：配置在运行中不会变，重复解析只是浪费。

    缓存键是传入的 `path`（通常是 None），而实际路径可能来自环境变量 ——
    这不成问题，因为环境变量只在启动时设置一次。若哪天真需要热重载，
    要动的是这里的缓存策略，而不是在调用点绕开它。
    """
    if type == "yaml":
        return YamlConfigLoader.get_config(path)

    raise ValueError("get_config | 暂不支持该类型配置加载")
