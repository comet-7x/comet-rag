from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .schemas import APPConfig


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
        yaml_path = path or cls.yaml_path

        if not Path(yaml_path).exists():
            return {}

        with open(yaml_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    @classmethod
    def get_config(cls, path: str | None = None) -> APPConfig:
        return APPConfig.model_validate(cls.load(path))


@lru_cache
def get_config(path: str | None = None, type: str = "yaml") -> APPConfig:
    if type == "yaml":
        return YamlConfigLoader.get_config(path)

    raise ValueError("get_config | 暂不支持该类型配置加载")
