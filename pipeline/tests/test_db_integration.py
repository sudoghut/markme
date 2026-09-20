"""对着**真实数据库**的集成测试（§9.3.3）。

§13 把「anon key 配 RLS 配错 → 数据可被任意写入」的缓解措施押在这一组上，
所以它必须真的连库跑，而不是断言 SQL 文本。

**没有凭证时整组跳过**，不是失败 —— 因为 fork PR 和本地 clone 都拿不到。

> **一条曾经写在这里的理由是错的，留个记号免得它再长回来。**
> 初稿说：anon key 按设计可公开，所以用**仓库 Variable** 而不是 Secret 提供，
> 「于是 fork PR 也能跑这组测试」。**后半句是假的。**
> GitHub 不把仓库 Variable 传给由 fork 的 pull_request 触发的工作流 ——
> 和 Secret 是同一条限制，只是 Variables 的文档页对此只字未提。
> 用 Variable 仍然是对的，但真正的理由只有一条：**Secret 会在日志里被打码**，
> 而这组测试排障时要看的就是 URL 和返回体。fork PR 这组必然跳过。

「跳过」在 CI 里是个陷阱：变量被误删、被改名、Supabase 项目被暂停，
`pytest` 都只会打印 `28 skipped`，**CI 依旧全绿**，而 §13 押注的这道防线
已经不再被验证 —— 而且（§7.2.1）恰恰是在没人盯着的稳定期悄悄消失。
所以在**凭证本来就该到位**的地方（push、schedule、本仓库自己的 PR）
置 ``SUPABASE_TESTS_REQUIRED=1``，此时缺凭证是失败而不是跳过；
fork PR 与本地 clone 不设它，照常跳过。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

import pytest

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")

#: CI 置 1。此时「没有凭证」是失败而不是跳过 —— 见模块 docstring。
REQUIRED = os.environ.get("SUPABASE_TESTS_REQUIRED", "").strip().lower() not in (
    "",
    "0",
    "false",
    "no",
)

#: 没有凭证时跳过。**加在每个类上，不加在模块上** —— 见下面那条守卫测试。
needs_creds = pytest.mark.skipif(
    not (SUPABASE_URL and ANON_KEY),
    reason="需要 SUPABASE_URL 与 SUPABASE_ANON_KEY 才能跑；fork PR 与本地 clone 正常跳过",
)


def test_credentials_are_present_when_required() -> None:
    """凭证该在而不在时，**这里**红 —— 一条测试，不是一次收集期崩溃。

    最初这段写成模块级的 ``pytest.fail``，那会让 pytest 以
    「Interrupted: 1 error during collection」退出码 2 收场，
    **另外 255 条与数据库毫无关系的测试一条都不跑**。
    一个缺失的仓库变量不该顺手把整次运行的信号也一起抹掉。
    """
    if not REQUIRED:
        pytest.skip("未置 SUPABASE_TESTS_REQUIRED；fork PR 与本地 clone 本来就该跳过")
    missing = [
        n for n, v in (("SUPABASE_URL", SUPABASE_URL), ("SUPABASE_ANON_KEY", ANON_KEY)) if not v
    ]
    assert not missing, (
        f"SUPABASE_TESTS_REQUIRED=1 但缺少 {', '.join(missing)}。"
        "§13 把「anon key 配 RLS 配错 → 数据可被任意写入」的缓解押在这组测试上，"
        "让它们静默跳过等于那道防线消失而 CI 全绿。"
        "两者都是仓库 **Variables**（不是 Secrets），请检查是否被删或改名。"
    )


# 每张表一份**结构上合法**的 payload、一个能命中 0 行的过滤器，
# 以及一个确实存在的列（给 PATCH 用）。
#
# **这不是为了好看。** 用一份通用的 {"symbol": "ZZZZ"} 去 POST
# trading_sessions 会得到 **400**（那张表没有 symbol 列）——
# 而 400 的意思是「请求格式不对」，不是「你被拒绝了」。
# 请求根本没走到授权层，于是即便 anon 拥有完全写权限，
# 断言也照样会通过。**那样的测试什么都没证明。**
WRITE_CASES: dict[str, dict[str, Any]] = {
    "trading_sessions": {
        "row": {"date": "1900-01-01", "ordinal": -1, "close_et": "16:00:00"},
        "filter": "date=eq.1900-01-01",
        "patch": {"is_half_day": True},
    },
    "symbols": {
        "row": {"symbol": "ZZZZ", "name": "hack", "type": "stock"},
        "filter": "symbol=eq.ZZZZ",
        "patch": {"name": "hacked"},
    },
    "symbol_events": {
        "row": {
            "symbol": "ZZZZ",
            "event_type": "earnings",
            "event_date": "1900-01-01",
            "is_estimated": False,
            "source": "yfinance",
        },
        "filter": "symbol=eq.ZZZZ",
        "patch": {"is_estimated": True},
    },
    "prices_daily": {
        "row": {
            "symbol": "ZZZZ",
            "date": "1900-01-01",
            "adj_close": 1.0,
            "source": "yfinance",
        },
        "filter": "symbol=eq.ZZZZ",
        "patch": {"volume": 1},
    },
    "metrics_daily": {
        "row": {"symbol": "ZZZZ", "date": "1900-01-01"},
        "filter": "symbol=eq.ZZZZ",
        "patch": {"rsi_14": 99.0},
    },
    "strength_daily": {
        "row": {
            "date": "1900-01-01",
            "symbol": "ZZZZ",
            "rank": 1,
            "score": 1.0,
            "score_metric": "mom_20",
            "rank_pool": "stocks",
            "benchmark": "QQQ",
            "top_n": 3,
            "in_top_n": True,
        },
        "filter": "symbol=eq.ZZZZ",
        "patch": {"rank": 2},
    },
}
PUBLIC_TABLES = tuple(WRITE_CASES)

# 断在 **SQLSTATE 上，不断在 HTTP 状态码上**。
#
# 42501 是 Postgres 自己的 insufficient_privilege，PostgREST 原样透出来 ——
# 它是授权层给出的答案本身，而 HTTP 状态码是 PostgREST 对它的翻译。
#
# 这个项目实测过一轮，值得记下来：
#
#   | 请求 | 状态 | body |
#   |---|---|---|
#   | 好 key，INSERT | **401** | `{"code":"42501", … "permission denied for table …"}` |
#   | 坏 key，INSERT | 401 | `{"message":"Invalid API key"}`（**没有 code 字段**） |
#   | 无 key，SELECT | 401 | `{"message":"No API key found in request"}` |
#   | payload 不对 | 400 | PGRST1xx |
#
# 也就是说：**这个部署返回的是 401 而不是 403**（设计文档里原先写的 403 是错的），
# 而两种 401 只能靠 body 分开，靠状态码分不开。断在 42501 上，
# 上面四行里只有第一行能过 —— 比任何一组状态码元组都严。
DENIED_SQLSTATE = "42501"


def _request(
    path: str, *, method: str = "GET", body: Any = None, profile: str | None = None
) -> tuple[int, str]:
    """以 anon 身份打 PostgREST。返回 ``(status, body)``，**不抛异常** ——
    这组测试要断言的正是那些非 2xx 的状态码。

    ``profile`` 填 PostgREST 的 ``Accept-Profile`` 头，用来**点名**一个 schema。
    不填时 PostgREST 只认 ``public``，于是对 ``private`` 下的表问什么都得 404，
    而那个 404 与「表名打错了」完全分不开。
    """
    headers = {
        "apikey": ANON_KEY,
        "Authorization": f"Bearer {ANON_KEY}",
        "Content-Type": "application/json",
    }
    if profile is not None:
        headers["Accept-Profile"] = profile
        headers["Content-Profile"] = profile
    # S310：URL 由 SUPABASE_URL 环境变量拼出，一律是我们自己项目的 https 端点，
    # 不接受外部输入的 scheme。这里就是要打真实 HTTP 才算集成测试。
    req = urllib.request.Request(  # noqa: S310
        f"{SUPABASE_URL}/rest/v1/{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method=method,
    )
    proxy = os.environ.get("HTTPS_PROXY")
    opener = (
        urllib.request.build_opener(urllib.request.ProxyHandler({"https": proxy, "http": proxy}))
        if proxy
        else urllib.request.build_opener()
    )
    try:
        with opener.open(req, timeout=60) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _assert_denied(table: str, verb: str, status: int, body: str) -> None:
    """断言请求**走到了授权层并被拒**。

    一个会「假通过」的测试比没有测试更糟，因为它会让人以为这道防线是活的。
    所以这里要的是授权层自己说的那句话（42501），而不是「反正不是 2xx」：
    payload 写错（400 / PGRST1xx）、key 过期（401 而无 code）、
    表被改名（404 / PGRST205）都拿不到 42501，于是都会红。
    **修请求或凭证，别放宽断言。**
    """
    try:
        code = json.loads(body).get("code")
    except (ValueError, AttributeError):
        code = None
    assert code == DENIED_SQLSTATE, (
        f"{table} 的 {verb} 返回 {status}，但 body 不是授权层的 {DENIED_SQLSTATE} 拒绝。"
        f"这条断言因此什么都没验证 —— 修请求或凭证，别放宽断言。body={body[:200]}"
    )
    # 状态码只作佐证：这个部署把 42501 翻译成 401。
    assert status in (401, 403), f"{table} 的 {verb} 状态码意外：{status} {body[:200]}"


@pytest.fixture(params=PUBLIC_TABLES)
def table(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@needs_creds
class TestAnonCanRead:
    """**逐一列举**六张表，不用循环里的一个断言。

    漏掉 ``trading_sessions`` 那一项时，它缺授权的话测试仍会通过，
    但 ``v_strength_enriched`` 会在生产上 join 不到它而崩 ——
    这正是上一轮 review 指出的那个洞。
    """

    def test_select_succeeds(self, table: str) -> None:
        status, body = _request(f"{table}?select=*&limit=1")
        assert status == 200, f"{table} 读失败：{status} {body[:200]}"

    def test_the_view_is_readable(self) -> None:
        """``v_strength_enriched`` 是 security_invoker 视图，
        它 join ``trading_sessions`` —— 缺后者的授权就会被拒。"""
        status, body = _request("v_strength_enriched?select=*&limit=1")
        assert status == 200, f"视图读失败：{status} {body[:200]}"


@needs_creds
class TestAnonCannotWrite:
    """**这一组是 §13 押注的那道防线。**

    GRANT 管命令级、RLS 管行级，两道都必须拒。
    """

    def test_insert_is_denied(self, table: str) -> None:
        status, body = _request(table, method="POST", body=WRITE_CASES[table]["row"])
        _assert_denied(table, "INSERT", status, body)

    def test_update_is_denied(self, table: str) -> None:
        case = WRITE_CASES[table]
        status, body = _request(f"{table}?{case['filter']}", method="PATCH", body=case["patch"])
        _assert_denied(table, "UPDATE", status, body)

    def test_delete_is_denied(self, table: str) -> None:
        case = WRITE_CASES[table]
        status, body = _request(f"{table}?{case['filter']}", method="DELETE")
        _assert_denied(table, "DELETE", status, body)

    def test_the_view_is_not_writable(self) -> None:
        """视图也要收权（§9.3 4.1）。

        这个洞是 ``invariants.sql`` 自己抓出来的：迁移第一次跑完，
        「anon 不得持有写权限」那条立刻报了 5 条 ``v_strength_enriched`` 的违规
        —— 我的 REVOKE 只列了六张表，漏了视图。

        **这条测试只能证明「写不进去」，证明不了「因为没权限」。**
        实测返回 500 / 55000（``Views containing WITH are not automatically
        updatable``）—— Postgres 的重写器在 ACL 检查**之前**就先拒了。
        所以权限那一侧由 ``invariants.sql`` 的「anon 不得持有写权限」覆盖：
        这里是皮带，那里是吊带，两条都要在。
        """
        status, body = _request(
            "v_strength_enriched", method="POST", body={"symbol": "ZZZZ", "rank": 1}
        )
        code = json.loads(body).get("code") if body else None
        # 钉死那个具体答案。`status >= 400` 会被视图改名（404）、key 过期（401）
        # 一起满足，于是这条测试在视图已经不存在时照样绿。
        assert code == "55000", f"视图的写入被拒，但不是因为它不可更新：{status} {body[:200]}"


@needs_creds
class TestPrivateSchemaIsUnreachable:
    """``private.runs`` 与 ``private.fetch_state`` 不走 RLS ——
    它们的保护来自「PostgREST 不暴露 private schema + 无 anon 授权」。

    §9.3.1 选这条路而不是 SECURITY DEFINER 视图，正是为了避免
    「有人把它改成 security_invoker 后视图静默返回 0 行」那种沉默故障。
    """

    @pytest.mark.parametrize("name", ["runs", "fetch_state"])
    def test_the_default_schema_does_not_serve_it(self, name: str) -> None:
        """不带 ``Accept-Profile`` 时 PostgREST 只认 ``public``，答案必须是
        「``public.<name>`` 不存在」。

        **断到 PGRST205，不断 ``status != 200``。** 后者被至少四种原因满足：
        表名打错、key 过期、Supabase 5xx —— 三种都与「private 是否可达」无关，
        而全都会让这条测试继续绿着。
        """
        status, body = _request(f"{name}?select=*&limit=1")
        code = json.loads(body).get("code") if body else None
        assert status == 404 and code == "PGRST205", (
            f"private.{name} 的默认 schema 探测返回了意外的答案：{status} {body[:200]}"
        )

    @pytest.mark.parametrize("name", ["runs", "fetch_state"])
    def test_naming_the_private_schema_does_not_help(self, name: str) -> None:
        """**这条才是能真的失败的那条。**

        上一条问的是「``public.runs`` 在不在」，而它当然不在 —— 表在 ``private``。
        这里用 ``Accept-Profile: private`` **点名**那个 schema，
        于是它真的去问「anon 能不能读 private.runs」。

        权限那一侧由 ``invariants.sql`` 的「anon 不得触及 private schema」覆盖；
        这里是皮带，那里是吊带。
        """
        status, body = _request(f"{name}?select=*&limit=1", profile="private")
        assert status != 200, f"private.{name} 竟然可达：{body[:200]}"
