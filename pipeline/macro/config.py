"""``config/macro.yaml`` 的模型。

单独成文件而不并进 :class:`pipeline.config.Config`：宏观管道与价格管道互不依赖，
价格管道的 ``load_config`` 不该因为这份可选板块的配置有误而整个起不来。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import Field, model_validator

from pipeline.config import CONFIG_DIR, _Strict
from pipeline.errors import ConfigError

__all__ = ["MacroConfig", "StaleAfterDays", "TenorSpec", "load_macro_config"]

PosInt = Annotated[int, Field(ge=1)]


class TenorSpec(_Strict):
    #: 财政部 CSV 里的列名，必须与官方表头逐字一致。
    column: str = Field(min_length=1)
    #: 库里 ``treasury_yields.tenor`` 的取值。
    key: str = Field(pattern=r"^[a-z0-9]+$")
    #: 页面上的短名。
    label: str = Field(min_length=1)


class StaleAfterDays(_Strict):
    target_range: PosInt
    yields: PosInt
    fomc: PosInt


class MacroConfig(_Strict):
    history_start: date
    treasury_tenors: list[TenorSpec] = Field(min_length=1)
    stale_after_days: StaleAfterDays
    request_interval_seconds: Annotated[float, Field(ge=0)]
    max_requests_per_run: PosInt
    retry_max_attempts: PosInt
    request_timeout_seconds: Annotated[float, Field(gt=0)]

    @model_validator(mode="after")
    def _unique_tenors(self) -> MacroConfig:
        keys = [t.key for t in self.treasury_tenors]
        cols = [t.column for t in self.treasury_tenors]
        if len(set(keys)) != len(keys):
            raise ConfigError(f"macro.yaml 的 treasury_tenors 有重复的 key：{keys}")
        if len(set(cols)) != len(cols):
            raise ConfigError(f"macro.yaml 的 treasury_tenors 有重复的 column：{cols}")
        return self


def load_macro_config(config_dir: Path | None = None) -> MacroConfig:
    path = (config_dir or CONFIG_DIR) / "macro.yaml"
    if not path.is_file():
        raise ConfigError(f"缺少配置文件：{path}")
    with path.open(encoding="utf-8") as fh:
        data: Any = yaml.safe_load(fh)  # 必须 safe_load
    if not isinstance(data, dict):
        raise ConfigError(f"macro.yaml 的顶层必须是映射，得到 {type(data).__name__}")
    try:
        return MacroConfig(**data)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"macro.yaml 校验失败：{exc}") from exc
