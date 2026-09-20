-- markme (market metrics) — 初始 schema
--
-- 对应设计文档 docs/create-project.md 的 §9.1 / §9.2.1 / §9.3。
--
-- ┌─ 执行前提（顺序不能反）─────────────────────────────────────────────┐
-- │ 1. 角色 pipeline_writer 必须**已经存在**。本文件只做授权，不含      │
-- │    create role，也不含任何密码。先跑一次（密码自己定）：            │
-- │        create role pipeline_writer login noinherit password '…';   │
-- │    反过来（先跑本文件）会让下面所有 grant … to pipeline_writer      │
-- │    直接失败并中止整个事务。                                        │
-- │ 2. **必须在库里还没有任何数据时执行**（§12 #9）。这是唯一一次       │
-- │    爆炸半径天然为零的机会，而它恰好是风险最高的一次迁移 ——         │
-- │    REVOKE / GRANT / RLS 全在里面。顺序固定为：                      │
-- │        建角色 → 本文件 → invariants.sql → 集成测试 → 然后才回填数据 │
-- └────────────────────────────────────────────────────────────────────┘
--
-- 回滚见同目录的 0001_rollback.sql。**没有回滚脚本的迁移不允许执行。**

begin;

-- ===========================================================================
-- 1. 表
-- ===========================================================================

-- 交易日历（由 pandas_market_calendars 的 XNAS schedule 落库，M4 的交付项）。
--
-- 没有这张表，SQL 侧就无从判断「上一行是不是上一个交易 session」—— 只有 date
-- 的话 Postgres 不知道 Juneteenth 和感恩节，而用工作日差替代会把交易所假日
-- 误判为连续，§4.2 的 rank_delta_1d / days_in_top_n 会静默算错。
--
-- 运维契约见 §9.1.4：左端点 sessions_start_date **固定、永不前移**。
-- ordinal 每次对账都重新推导，整套方案的正确性全押在左端点不动上。
create table trading_sessions (
  date        date primary key,
  ordinal     int  not null unique,    -- 连续序号：相邻 session 的差恒为 1
  is_half_day boolean not null default false,
  close_et    time not null            -- 16:00 或 13:00，§7.2 闸门 2 直接读它
);

-- 标的元数据（由 config/universe.yaml 同步而来，M4 的交付项）。
create table symbols (
  symbol           text primary key check (symbol = upper(symbol)),
  name             text not null,
  type             text not null check (type in ('stock','etf')),
  is_benchmark     boolean not null default false,
  -- 移出 config 的标的走软删除：prices_daily 对本表有外键，真删会被挡住，
  -- 而且历史数据必须保留（否则回补与历史榜单都会断）。
  enabled          boolean not null default true,
  -- ETF 永远没有财报。§3.5(5) 的新鲜度断言靠它把 QQQ 排除在外 ——
  -- 否则那条断言从第一天起就是红的，而长期飘红的断言等于没有断言。
  expects_earnings boolean not null default true,
  updated_at       timestamptz not null default now()
);

-- 公司事件（§3.5）。
--
-- **无自然主键是故意的**：供应商不提供稳定的事件 id，而
-- (symbol, event_type, event_date) 也不唯一 —— 常规分红与特别分红可以
-- 共用同一个除息日。幂等性来自「抓取成功后整窗删+插」，不是主键冲突，
-- 与 strength_daily（§9.1.2）同一套路。
create table symbol_events (
  id           bigint generated always as identity primary key,
  symbol       text not null references symbols(symbol),
  event_type   text not null check (event_type in ('earnings','dividend')),
  event_date   date not null,
  -- 无 default：写入方必须显式表态（§3.5(2)）。把已发生的除息日标成「估计」
  -- 是另一种不诚实，而且会让前端的虚线到处都是、从而失去意义。
  is_estimated boolean not null,
  -- 口径：Ticker.dividends 返回的是**拆股调整后**的每股金额，与公告原值
  -- 在有拆股的窗口里不一致（NVDA/AVGO 2024 均 10:1）。本项目只存前者。
  amount       numeric(14,6) check (event_type = 'dividend' or amount is null),
  source       text not null check (source in ('yfinance','stooq')),
  updated_at   timestamptz not null default now()
);
create index on symbol_events (symbol, event_type, event_date);
create index on symbol_events (event_type, event_date);  -- 跨标的新鲜度断言走这条

-- 日线价格（原始事实层，保留以便任何时候重算指标）。
create table prices_daily (
  symbol     text not null references symbols(symbol),
  date       date not null,
  open       numeric(14,4),
  high       numeric(14,4),
  low        numeric(14,4),
  -- close 可空：Stooq 的价格本身就是已复权的、且没有独立的复权列，
  -- 所以备源只写 adj_close（§3.0 规则 3）。adj_close 则一定有值 ——
  -- 所有指标都用它。
  close      numeric(14,4) check (close > 0),
  adj_close  numeric(14,4) not null check (adj_close > 0),
  -- §3.0 规则 2 的比对依据：复权因子会被供应商**追溯改写**，
  -- 日常运行拿它比对「昨日行的因子是否变了」。
  -- 注意这个探测器只对 yfinance 有效 —— Stooq 行的 close 为 NULL，
  -- 生成列随之为 NULL，实现时不得把「NULL 比 NULL」当成「没变」。
  adj_factor numeric(18,10) generated always as (adj_close / nullif(close, 0)) stored,
  volume     bigint,
  source     text not null check (source in ('yfinance','stooq')),
  updated_at timestamptz not null default now(),
  primary key (symbol, date)
);
create index on prices_daily (date desc);

-- 指标（派生层，可随时由 prices_daily 全量重算）。
create table metrics_daily (
  symbol                     text not null references symbols(symbol),
  date                       date not null,

  -- ── 核心指标：显式列，便于索引与排序 ──────────────────────────────
  rsi_14                     numeric(8,4),   -- 有界 0–100，定精度安全
  ema_60                     numeric(14,4),
  close_vs_ema60_pct         numeric(10,6),
  ema60_slope_20d            numeric(10,6),
  -- alpha / 残差波动没有自然上界 → **不定精度**。
  -- numeric(10,6) 上限是 9999.999999，一旦溢出，Postgres 中止的是**整批
  -- insert** —— 「一个坏格子毁掉一整天的数据」，对无界列是很差的交换。
  alpha_annual               numeric,
  beta                       numeric(10,6),
  r2                         numeric(8,6),
  corr                       numeric(8,6),
  resid_vol_annual           numeric,
  alpha_t_stat               numeric(10,4),
  n_obs                      int,
  mom_20                     numeric(10,6),

  -- ── 事件距离（§3.5）。单位是**日历日**，与全项目其余窗口的交易日单位
  --    由列名 days_* / sessions_* 隔开。
  --    **只在最新一个 session 的行上有值，历史行一律 NULL**（§3.5(3)）：
  --    事件改期不会让「当时的预告是 28 天后」变成假的，每天重写会让同一条
  --    历史行今天显示 28、明天显示 35，永不稳定。
  days_to_next_earnings      int,
  days_since_last_earnings   int,
  days_to_next_dividend      int,
  days_since_last_dividend   int,
  next_earnings_date         date,   -- 存日期才能渲染 tooltip，且让数字可审计
  next_dividend_date         date,
  next_earnings_is_estimated boolean,
  next_dividend_is_estimated boolean,

  -- 未来新增指标的落脚点：加非核心指标不必做 schema 迁移（§9.2）。
  extra                      jsonb not null default '{}'::jsonb,
  -- 预热标记是**按指标**而非按行的（§3.3）：一个只有 170 根 bar 的标的，
  -- RSI 可信而 EMA 尚未收敛。行级 boolean 只能在「整行打灰」和「都不打灰」
  -- 之间二选一，两个都是错的。
  provisional_metrics        text[] not null default '{}',
  computed_at                timestamptz not null default now(),
  primary key (symbol, date)
);
create index on metrics_daily (date desc);

-- 每日完整排名（全量历史，全池全部名次，见 §4.4）。
create table strength_daily (
  date            date not null,
  symbol          text not null references symbols(symbol),
  rank            int  not null check (rank >= 1),
  score           numeric(12,6) not null,

  -- 当时生效的**全部**排名口径。缺一则历史行不可比且无迹可循 ——
  -- 在 stocks 池里排第 2 和在 all 池里排第 2 不是一回事。
  score_metric    text not null,
  rank_pool       text not null check (rank_pool in ('stocks','all')),
  benchmark       text not null references symbols(symbol),
  top_n           int  not null check (top_n >= 1),
  in_top_n        boolean not null,  -- 写入时由 pipeline 判定，视图不必知道 top_n

  delta_to_next   numeric(12,6),
  delta_to_median numeric(12,6),

  -- 主键是 (date, symbol) 而不是 (date, rank)：后者下没有任何东西阻止
  -- 同一标的出现在两个名次上，而 (date, rank) 的 upsert 检测不到。
  primary key (date, symbol),
  unique (date, rank)                -- 同一天同一名次只能有一个标的
);
-- v_strength_enriched 的窗口按 symbol 分区并按 session 排序，
-- 主键 (date, symbol) 对它是反向的。
create index on strength_daily (symbol, date desc);

-- ── private schema：PostgREST 不暴露它 ────────────────────────────────
--
-- runs 放这里而不是用一个 SECURITY DEFINER 视图把三列暴露给 anon（§9.3.1）：
-- 那样会引入本项目唯一的 SECURITY DEFINER 对象、一条 Security Advisor 告警，
-- 以及一个「有人把它改成 security_invoker 后视图静默返回 0 行」的失败模式。
-- 前端的数据新鲜度改由 select max(date) from metrics_daily 得到 ——
-- 一张 anon 本来就在读的表。
create schema private;

create table private.runs (
  id           bigint generated always as identity primary key,
  session_date date,
  -- 枚举值由数据库强制，不是靠注释。一个 'sucess' 的拼写错误会让前端判活
  -- 失败、黄条亮起，而你会先去查错层。
  --
  -- running：**开跑即插**。status not null 若意味着行是在结束时插入的，
  -- 那么 runner OOM / 超时 / 被取消时一行都不会留下 —— 可观测性表对硬崩溃
  -- 完全失明。它必须在 T1 里**单独提交**（§9.1.2），否则会跟着 T2 一起回滚。
  status       text not null check (status in (
                 'running','ok','skipped_holiday','skipped_too_early',
                 'skipped_already_done','ok_events_stale',
                 'stale_vendor','partial','failed')),
  started_at   timestamptz not null,
  finished_at  timestamptz,
  rows_prices  int,
  rows_metrics int,
  git_sha      text,
  message      text
);

-- 事件抓取的节奏状态（§3.5(4)）。
-- 不能依赖 symbol_events.updated_at —— 那张表走整窗删+插，它记的是
-- 「最近一次写入」而不是「最近一次尝试抓取」，而抓取成功但内容没变时，
-- 我们仍然需要知道「我今天查过了」。
create table private.fetch_state (
  symbol              text primary key references symbols(symbol),
  last_event_fetch_at timestamptz
);

-- ===========================================================================
-- 2. 视图
-- ===========================================================================

-- rank_delta_1d 与 days_in_top_n **不入库**，读取时用窗口函数现算（§4.2）。
-- 它们是「其他行的函数」：一旦反规范化进每一行，§3.0 规则 2 的回补重算改了
-- 某个历史日的名次后，其后每一天的连续计数链就全错了，而没有任何东西会
-- 重算它们 —— 卡片上那个「在榜 5 天」会悄悄撒谎。
--
-- security_invoker = true：以调用者身份读，沿用 strength_daily 的 RLS。
create view v_strength_enriched with (security_invoker = true) as
with b as (
  select s.*,
         ts.ordinal,
         lag(s.rank)     over w as prev_rank,
         lag(ts.ordinal) over w as prev_ord,
         lag(s.in_top_n) over w as prev_in
  from strength_daily s
  join trading_sessions ts on ts.date = s.date
  -- 必须按**全部排名口径字段**分区，否则跨配置变更会编造出假的名次变动。
  window w as (
    partition by s.symbol, s.score_metric, s.rank_pool, s.benchmark, s.top_n
    order by ts.ordinal
  )
),
g as (
  select b.*,
         sum(case when prev_ord is null           -- 首行
                    or ordinal - prev_ord <> 1    -- 跨了非连续 session（长假 / 缺数据）
                    or prev_in is not true        -- 昨天不在榜
                    or in_top_n is not true       -- 今天不在榜
                  then 1 else 0 end)
           over (partition by symbol, score_metric, rank_pool, benchmark, top_n
                 order by ordinal rows unbounded preceding) as streak_id
  from b
)
select date, symbol, rank, score, score_metric, rank_pool, benchmark, top_n,
       in_top_n, delta_to_next, delta_to_median,
       -- 只有相邻 session 之间才谈得上「名次变动」。
       -- 连续性判据是 ordinal 差 1，**不是日期相减** —— 这正是需要
       -- trading_sessions.ordinal 的原因，仅凭 date 做不到。
       case when prev_ord is not null and ordinal - prev_ord = 1
            then prev_rank - rank end as rank_delta_1d,
       case when in_top_n then
              row_number() over (partition by symbol, score_metric, rank_pool,
                                              benchmark, top_n, streak_id
                                 order by ordinal)
            end as days_in_top_n
from g;

-- ===========================================================================
-- 3. 触发器
-- ===========================================================================
--
-- 列的 DEFAULT now() **只在 INSERT 时生效**。ON CONFLICT … DO UPDATE 不会
-- 重新触发它 —— 于是你在排查「这行是不是陈旧了」时，唯一会去看的那一列
-- 恰好在撒谎。用触发器而不是在每条 upsert 里手写 set …，因为触发器扛得住
-- 「有人忘了写」。

create function touch_updated_at() returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end
$$;

create function touch_computed_at() returns trigger language plpgsql as $$
begin
  new.computed_at = now();
  return new;
end
$$;

create trigger t_symbols_touch before update on symbols
  for each row execute function touch_updated_at();
create trigger t_prices_touch before update on prices_daily
  for each row execute function touch_updated_at();
create trigger t_events_touch before update on symbol_events
  for each row execute function touch_updated_at();
-- metrics_daily 用的是 computed_at 而不是 updated_at —— 别共用一个触发器，
-- 那会让迁移直接失败。
create trigger t_metrics_touch before update on metrics_daily
  for each row execute function touch_computed_at();

-- ===========================================================================
-- 4. 访问控制：GRANT 与 RLS 是**两道**，不是一道
-- ===========================================================================
--
-- 在 Postgres 里 GRANT 授权的是**命令**，RLS 过滤的是**行**，两者都必须满足。
-- 只写 policy 不写 grant，今天能跑纯粹是因为 Supabase 建项目时执行过
--   alter default privileges in schema public grant all on tables
--     to postgres, anon, authenticated;
-- 于是在 SQL Editor 里以 postgres 建的表隐式继承了授权。这带来两个缺陷：
--
-- (a) 缺失的 GRANT 是一颗前端定时炸弹：一旦那套默认权限变了，或某次迁移
--     由另一个角色执行，前端会以 42501 permission denied 直接死掉，
--     而没有任何一句话能解释为什么。
-- (b) 同一套隐式默认意味着 anon 此刻对六张表都持有 INSERT/UPDATE/DELETE
--     授权。「不给策略 → 默认拒绝」只在 RLS 开着且没有放行写策略时成立，
--     **完全没有纵深防御** —— 只要有人敲一句 disable row level security，
--     任何持有（按设计可公开的）anon key 的人就能 DELETE FROM prices_daily。

-- 4.1 先收权
--
-- alter default privileges 只对「执行它的那个角色此后创建的对象」生效，
-- 不会追溯修正其他角色的默认权限 → 必须固定一个迁移属主角色并写明 FOR ROLE。
alter default privileges for role postgres in schema public
  revoke all on tables from anon, authenticated;

-- **序列也在那套隐式默认权限里，而只收 tables 收不到它。**
-- symbol_events.id 是 `generated always as identity`，它背后有一条序列，
-- 而 `all on sequences` 含 UPDATE —— 也就是 setval。任何持有
-- （按设计可公开的）anon key 的人都能把序列拨回去，于是写入侧的下一次
-- INSERT 撞主键冲突。那不是「数据被改写」，而是**写入管线开始失败**，
-- 排查时没有任何线索指向权限。
alter default privileges for role postgres in schema public
  revoke all on sequences from anon, authenticated;

-- 函数一并收，但**要清楚它只关了两扇门里的一扇**：Postgres 对新函数默认
-- 授予 PUBLIC 的 EXECUTE，而 PUBLIC 里含 anon，revoke … from anon 不会
-- 撤掉那一份。真正的约束是别在 public schema 里建 SECURITY DEFINER 函数
-- （§9.3.1 选择不用 SECURITY DEFINER 视图，正是同一条理由）。
-- 这里写上它，是为了让「将来有人显式 grant … to anon」这件事至少不会
-- 因为默认权限而**无声发生**。
alter default privileges for role postgres in schema public
  revoke all on functions from anon, authenticated;

revoke all on trading_sessions, symbols, symbol_events,
              prices_daily, metrics_daily, strength_daily
  from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;

-- **视图也要收权。** 它不在上面那张表清单里，而 Supabase 的隐式默认权限
-- 同样给了 anon 对它的 ALL。这个视图带窗口函数与 CTE，因此不是
-- auto-updatable，写入本来就会失败 —— 但 §9.3 的整个论点就是
-- 「GRANT 是命令级的第一道」，不该依赖「它碰巧写不进去」。
--
-- 这个洞是 invariants.sql 的「anon 不得持有写权限」那条自己抓出来的：
-- 迁移第一次跑完，它立刻报了 5 条 v_strength_enriched 的违规。
revoke all on v_strength_enriched from anon, authenticated;

-- 4.2 再显式授予前端需要的读
--
-- 必须含 trading_sessions：v_strength_enriched 是 security_invoker 视图，
-- 以调用者身份 join 它，缺授权会被拒。
--
-- 先显式给 schema 的 USAGE。今天它是有的 —— Supabase 建项目时给过，
-- 而且 Postgres 的 public schema 历来对 PUBLIC 开放 USAGE。但这两条都是
-- **别人的默认值**：PG15 起 public schema 的默认权限已经收紧过一轮，
-- 而 §9.3 (a) 的论点就是「缺失的 GRANT 是一颗前端定时炸弹」——
-- 没有 USAGE 时，上面那些 `grant select` 一条都不生效，前端拿到 42501，
-- 而没有任何一句话能解释为什么。写出来它就不再取决于别人的默认值。
grant usage on schema public to anon;

grant select on trading_sessions, symbols, symbol_events,
                prices_daily, metrics_daily, strength_daily
  to anon;
grant select on v_strength_enriched to anon;

-- 4.3 RLS 作为第二道（六张表，一张都不能漏）
alter table trading_sessions enable row level security;
alter table symbols          enable row level security;
alter table symbol_events    enable row level security;
alter table prices_daily     enable row level security;
alter table metrics_daily    enable row level security;
alter table strength_daily   enable row level security;

create policy "public read" on trading_sessions for select to anon using (true);
create policy "public read" on symbols          for select to anon using (true);
create policy "public read" on symbol_events    for select to anon using (true);
create policy "public read" on prices_daily     for select to anon using (true);
create policy "public read" on metrics_daily    for select to anon using (true);
create policy "public read" on strength_daily   for select to anon using (true);

-- **不给 anon 任何 insert/update/delete 策略** → 默认拒绝。
--
-- 不加 FORCE ROW LEVEL SECURITY：FORCE 影响的是**表属主**，而
-- pipeline_writer 不是属主、本来就受 RLS 约束。运行时不会用属主身份，
-- 所以 FORCE 在这里不增加任何防护，只增加策略维护的失败面。

-- ===========================================================================
-- 5. 写入角色的授权（角色本身由前提步骤创建，见文件顶部）
-- ===========================================================================
--
-- 不用 service_role（§8.1.1）：它绕过 RLS、等同 DB 超管，而这个 job 会
-- pip install 约 80 个传递依赖。一旦某个包在 import 期读走 os.environ，
-- 攻击者就拿到了对现有及未来所有表的完全读写删权限，而 §13 对
-- 「数据可被任意写入」的缓解措施（RLS）在 service_role 面前**按设计无效**。

grant usage on schema public to pipeline_writer;

-- 逐表给**恰好需要的动词**。需要 DELETE 的只有三张：
--   · strength_daily   —— §9.1.2 的删+插重写
--   · trading_sessions —— §9.1.4 的全量对账（ordinal 每次重新推导）
--   · symbol_events    —— §3.5(1) 的整窗删+插
-- 这三张都可从上游完整再生，删得起。
-- prices/metrics/symbols 是**原始事实层与可重算派生层**，写入角色被攻陷时
-- 不该有能力把它们抹掉 —— 那正是本节要缩小的爆炸半径。
-- **不给序列任何授权，这是对的，不是漏的。**
--
-- `symbol_events.id` 与 `private.runs.id` 是 `generated always as identity`。
-- 和 `serial` 不同，identity 列的 nextval 是由执行器内部求值的
-- （PG10+ 把它改写成 NextValueExpr，`check_permissions = false`），
-- **不查调用者对序列的 USAGE**。
--
-- 实测（以 pipeline_writer 的真实连接）：
--   has_sequence_privilege('pipeline_writer', 'public.symbol_events_id_seq', 'USAGE') -> false
--   has_sequence_privilege('pipeline_writer', 'private.runs_id_seq',        'USAGE') -> false
--   而两张表的 INSERT 都成功。
--
-- 写在这里是因为这一点被 review 误报过一次（「writer 无法 INSERT identity 表」）。
-- 多给一条 `grant usage on sequence` 不会让任何事情变好，只会白白扩大
-- 写入凭证的爆炸半径 —— 而缩小它正是 §8.1.1 整节的目的。
grant select, insert, update         on symbols, prices_daily, metrics_daily
  to pipeline_writer;
grant select, insert, update, delete on strength_daily   to pipeline_writer;
grant select, insert, update, delete on trading_sessions to pipeline_writer;
grant select, insert, update, delete on symbol_events    to pipeline_writer;

-- **视图也要给 writer 读。** 它不在上面那张表清单里，而 §9.3.2 有两条不变式
-- 查 `v_strength_enriched`，keepalive.yml 是以 **pipeline_writer** 身份跑它们的。
--
-- 漏掉这一条的后果实测过：那两条断言以 postgres 跑全过，换成 writer 立刻变成
--   「断言本身执行失败：permission denied for view v_strength_enriched」。
-- 好在 run_invariants 把「断言自身报错」也算违规 —— 一个跑不起来的探测器
-- 和一个不触发的探测器一样没用 —— 所以它会**红**而不是静默放行。
-- 这仍然只是读权限：视图底下那几张表 writer 本来就能 select。
grant select on v_strength_enriched to pipeline_writer;

-- private schema 不走 RLS：它的保护来自「PostgREST 不暴露 + 无 anon 授权」。
grant usage on schema private to pipeline_writer;
grant select, insert, update on private.runs, private.fetch_state
  to pipeline_writer;

-- RLS 策略必须与上面的动词一一对应（**六张表都要**）。
-- 漏掉任何一张的 writer 策略，RLS 默认拒绝 → INSERT 报
-- "new row violates row-level security policy" → 那是在 T2 里（§9.1.2）
-- → **整个当日事务回滚、当天零数据**。下面是完整清单，不用省略号。

create policy "writer sel" on symbols for select to pipeline_writer using (true);
create policy "writer ins" on symbols for insert to pipeline_writer with check (true);
create policy "writer upd" on symbols for update to pipeline_writer using (true);

create policy "writer sel" on prices_daily for select to pipeline_writer using (true);
create policy "writer ins" on prices_daily for insert to pipeline_writer with check (true);
create policy "writer upd" on prices_daily for update to pipeline_writer using (true);

create policy "writer sel" on metrics_daily for select to pipeline_writer using (true);
create policy "writer ins" on metrics_daily for insert to pipeline_writer with check (true);
create policy "writer upd" on metrics_daily for update to pipeline_writer using (true);

create policy "writer sel" on strength_daily for select to pipeline_writer using (true);
create policy "writer ins" on strength_daily for insert to pipeline_writer with check (true);
create policy "writer upd" on strength_daily for update to pipeline_writer using (true);
create policy "writer del" on strength_daily for delete to pipeline_writer using (true);

create policy "writer sel" on trading_sessions for select to pipeline_writer using (true);
create policy "writer ins" on trading_sessions for insert to pipeline_writer with check (true);
create policy "writer upd" on trading_sessions for update to pipeline_writer using (true);
create policy "writer del" on trading_sessions for delete to pipeline_writer using (true);

create policy "writer sel" on symbol_events for select to pipeline_writer using (true);
create policy "writer ins" on symbol_events for insert to pipeline_writer with check (true);
create policy "writer upd" on symbol_events for update to pipeline_writer using (true);
-- symbol_events 的 delete 用 using (true)，与另两张一致：它可从供应商完整
-- 再生（Ticker.dividends 给全历史），删得起。**不要写成受限谓词** ——
-- 那会让 §3.5(1) 的整窗删+插静默删不完，反而留下孤儿行。
create policy "writer del" on symbol_events for delete to pipeline_writer using (true);

-- PostgREST 的 schema 缓存不会自己发现新建的对象。
notify pgrst, 'reload schema';

commit;
