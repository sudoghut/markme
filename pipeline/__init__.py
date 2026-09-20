"""markme (market metrics) — 收盘后指标管道。

设计见 ``docs/create-project.md``。本包只负责「抓取 → 计算 → 写库」，
不含任何前端逻辑，也不依赖 ``../low-buy``（AGENTS.md「独立性」）。
"""

__all__ = ["__version__"]

# 这是版本号的**唯一**来源；pyproject.toml 用 [tool.hatch.version] 从这里读。
__version__ = "0.1.0"
