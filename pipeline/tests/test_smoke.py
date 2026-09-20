"""M0 冒烟测试：证明工具链本身是通的。

这里**故意**不测任何业务逻辑 —— M0 的验收标准就是
「pytest / ruff / mypy 在空项目上绿」（§11）。
真正的指标测试从 M2 开始。
"""

from __future__ import annotations

from importlib.metadata import version

import pipeline


def test_package_importable_and_installed() -> None:
    """包能 import，且已作为 ``markme`` 安装。

    断言比的是 ``pipeline.__version__`` 与**安装元数据**里的版本 ——
    而不是两个写死的字面量。后者永远相等，测不出任何东西；
    前者能在有人只改了一处版本号时变红。
    """
    assert pipeline.__version__ == version("markme")
