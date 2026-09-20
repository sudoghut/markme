-- 线上数据库状态的不变式。
--
-- **每一条都必须返回 0 行。** 返回了行就是违规，调用方据此失败退出。
--
-- 为什么不只挂在 ci.yml 上（§12 #9 第 3 条）：`ci.yml` 的触发是 push + PR，
-- 而这些断言断的是**线上数据库状态**，不是代码。§7.2.1 已经指出这个项目的
-- 稳态是「跑得很好、没人再推代码」—— 于是「有人在 SQL Editor 里关掉了 RLS」
-- 「Security Advisor 的自动修复删掉了一条授权」这类探测器，**恰好在没人盯着
-- 的时候停止运行**。所以它同时挂在 keepalive.yml（按计划跑、本来就连库）上。
--
-- ┌─ 这些查询必须与执行它们的角色无关 ─────────────────────────────────┐
-- │ keepalive.yml 用的是 **pipeline_writer**，不是 postgres。          │
-- │                                                                    │
-- │ 初版的两条安全断言查 information_schema.role_table_grants，        │
-- │ 那个视图对调用者是**有过滤的**（只显示调用者是 grantor 或 grantee  │
-- │ 成员的授权）。实测：给 anon 注入一条 `grant insert on prices_daily`，│
-- │   以 postgres 跑         → 抓到 1 行                               │
-- │   以 pipeline_writer 跑  → **0 行，静默漏过**                      │
-- │ 也就是说，这条探测器在手动跑时好好的，一挂上定时任务就永久失明 ——  │
-- │ 正是 §9.3.1 论证要避免的那种沉默故障，落在了探测器自己身上。       │
-- │                                                                    │
-- │ 改用 pg_catalog 的 has_table_privilege / has_schema_privilege：    │
-- │ 它们与调用者身份无关，且能覆盖经由 PUBLIC 继承来的权限。           │
-- └────────────────────────────────────────────────────────────────────┘
--
-- 每条断言用 `-- name: …` 标注，跑它的脚本按这个切分并逐条报告。

-- ===========================================================================
-- A. 结构与权限 —— 与数据无关，空库上也必须通过
-- ===========================================================================

-- name: public 下的每张表都必须开启 RLS
--
-- Supabase 仪表盘的 Table Editor 默认勾选「Enable RLS」，但在 SQL Editor 里
-- `create table` **不会**。叠加「anon 隐式持有 ALL」，任何在 0002+ 迁移里
-- 新增的表都会带着「RLS 关闭 + anon 可写」上线，而 §11.5 的 review 闸门
-- 审的是**代码 diff，不是线上数据库状态**。
--
-- relkind 含 'p'：分区表也是表，而这条断言说的是「任何新增的表」。
select c.relname as violation
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public'
  and c.relkind in ('r', 'p')
  and not c.relrowsecurity;

-- name: anon 与 authenticated 不得对任何表或视图持有写权限
--
-- 「不给策略 → 默认拒绝」只在 RLS 开着且没有放行写策略时成立。
-- 这条查的是更底下的那一层：命令级授权本身。
--
-- 含 relkind 'v'：**视图也要收权**。这个洞是本条断言自己抓出来的 ——
-- 迁移第一次跑完它就报了 5 条 v_strength_enriched 的违规，
-- 因为我的 REVOKE 只列了六张表，漏了视图。
select r || ' -> ' || n.nspname || '.' || c.relname || ' (' || p || ')' as violation
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
cross join unnest(array['anon', 'authenticated']) r
cross join unnest(array['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES']) p
where n.nspname in ('public', 'private')
  and c.relkind in ('r', 'p', 'v', 'm', 'f')
  and has_table_privilege(r, c.oid, p);

-- name: anon 与 authenticated 不得触及 private schema
--
-- private 的保护不来自 RLS，而来自「PostgREST 不暴露它 + 无 anon 授权」。
-- 所以这里既要查 schema 级的 usage，也要查表级的任何权限。
select r || ' -> ' || obj as violation
from (
  select r, 'schema private (' || p || ')' as obj
  from unnest(array['anon', 'authenticated']) r
  cross join unnest(array['USAGE', 'CREATE']) p
  where has_schema_privilege(r, 'private', p)
  union all
  select r, 'private.' || c.relname || ' (' || p || ')'
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
  cross join unnest(array['anon', 'authenticated']) r
  cross join unnest(array['SELECT', 'INSERT', 'UPDATE', 'DELETE']) p
  where n.nspname = 'private'
    and c.relkind in ('r', 'p', 'v', 'm')
    and has_table_privilege(r, c.oid, p)
) t;

-- name: anon 与 authenticated 不得对 public 下的序列持有写权限
--
-- Supabase 的项目引导不只给表授默认权限，**序列与函数也给**。
-- 迁移里只 revoke 了 tables 的话，anon 仍持有 symbol_events_id_seq 的
-- USAGE/SELECT/UPDATE。今天经 PostgREST 够不到，所以不可利用 ——
-- 但 §9.3 整节的论点就是「别指望某个隐式默认停在你发现它的地方」。
select r || ' -> ' || c.relname || ' (' || p || ')' as violation
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
cross join unnest(array['anon', 'authenticated']) r
cross join unnest(array['USAGE', 'UPDATE']) p
where n.nspname = 'public'
  and c.relkind = 'S'
  and has_sequence_privilege(r, c.oid, p);

-- name: public 下不得存在 SECURITY DEFINER 函数
--
-- §4.1 那段「函数的默认权限只关了两扇门里的一扇」结尾说：真正的约束是
-- **别在 public schema 里建 SECURITY DEFINER 函数**（Postgres 对新函数默认
-- 授予 PUBLIC 的 EXECUTE，而 PUBLIC 含 anon，revoke … from anon 撤不掉那一份）。
-- §9.3.1 选择不用 SECURITY DEFINER 视图也是同一条理由。
--
-- 但在此之前**没有任何东西在执行这条约束** —— 它只是一句注释。
select n.nspname || '.' || p.proname as violation
from pg_proc p
join pg_namespace n on n.oid = p.pronamespace
where n.nspname = 'public'
  and p.prosecdef;

-- name: 必需的授权不得消失 —— anon 的读与 pipeline_writer 的写
--
-- 上面那些断言**全是否定式**的（「anon 不得持有 X」）。但本文件开头列的两个
-- 动机里，第二个是「Security Advisor 的自动修复**删掉了**一条授权」——
-- 而那件事发生时，上面每一条都会安安静静地继续通过。
--
-- 少了 anon 的 SELECT，前端全站 42501；少了 writer 的 INSERT，
-- 每天的 T2 事务整个回滚、当天零数据（§9.1.2）。两者都是沉默的 ——
-- 前者只有访客看得见，后者只有对着空图表的人看得见。
--
-- 用 has_*_privilege 而不是查 information_schema：与调用者身份无关（见文件头）。
select t.role || ' 缺 ' || t.priv || ' on ' || t.rel as violation
from (values
  ('anon',            'trading_sessions',    'SELECT'),
  ('anon',            'symbols',             'SELECT'),
  ('anon',            'symbol_events',       'SELECT'),
  ('anon',            'prices_daily',        'SELECT'),
  ('anon',            'metrics_daily',       'SELECT'),
  ('anon',            'strength_daily',      'SELECT'),
  ('anon',            'v_strength_enriched', 'SELECT'),
  -- 视图：keepalive 以 writer 身份跑本文件，而下面有两条断言查它。
  -- 漏掉这一条时，那两条会以「断言本身执行失败」报出来（实测过）。
  ('pipeline_writer', 'v_strength_enriched', 'SELECT'),
  ('pipeline_writer', 'private.runs',        'SELECT'),
  ('pipeline_writer', 'private.runs',        'INSERT'),
  ('pipeline_writer', 'private.runs',        'UPDATE'),
  ('pipeline_writer', 'private.fetch_state', 'SELECT'),
  ('pipeline_writer', 'private.fetch_state', 'INSERT'),
  ('pipeline_writer', 'private.fetch_state', 'UPDATE'),
  -- DELETE 只给这三张（§8.1.1）；另三张**不该**有，由上面的否定式断言管。
  ('pipeline_writer', 'strength_daily',      'DELETE'),
  ('pipeline_writer', 'trading_sessions',    'DELETE'),
  ('pipeline_writer', 'symbol_events',       'DELETE')
) as t(role, rel, priv)
where not has_table_privilege(t.role, t.rel, t.priv)

union all

select 'pipeline_writer 缺 ' || p || ' on ' || rel
from unnest(array['trading_sessions', 'symbols', 'symbol_events',
                  'prices_daily', 'metrics_daily', 'strength_daily']) rel
cross join unnest(array['SELECT', 'INSERT', 'UPDATE']) p
where not has_table_privilege('pipeline_writer', rel, p)

union all

select r || ' 缺 USAGE on schema public'
from unnest(array['anon', 'pipeline_writer']) r
where not has_schema_privilege(r, 'public', 'USAGE')

union all

select 'pipeline_writer 缺 USAGE on schema private'
where not has_schema_privilege('pipeline_writer', 'private', 'USAGE');

-- ===========================================================================
-- B. 数据一致性
-- ===========================================================================
--
-- 上面那些全是「够不够得着」的断言，**一个返回 0 行的视图能全部通过**。
-- §9.3.1 已经为它删掉的那个视图诊断过这个失败模式（「返回 0 行且不报错 ……
-- 响亮的失败很便宜，沉默的失败很贵」），但那段推理没有被搬到
-- v_strength_enriched 上 —— 而它现在是唯一的视图、面向 anon、
-- 且依赖一个每次运行都被删掉重建的表的 INNER join。

-- name: 每个 strength_daily.date 都必须在 trading_sessions 里存在
--
-- 等价于「v_strength_enriched 的行数 == strength_daily 的行数」。
-- 这条查询就是 §9.1.4 那个静默故障的探测器：若 sessions_start_date 前移，
-- INNER join 会无声吞掉老行，而幸存的第一行 prev_ord 为 NULL
-- → days_in_top_n 悄悄归 1，卡片上「在榜 5 天」开始撒谎。
select s.date::text || ' ' || s.symbol as violation
from strength_daily s
left join trading_sessions ts on ts.date = s.date
where ts.date is null;

-- name: trading_sessions 的 ordinal 必须无缺口
--
-- `ordinal int unique` 只保证唯一，**不保证无缺口**。而「相邻 session」
-- 这个判据完全建立在「差 1」之上。
select prev_date::text || ' -> ' || date::text as violation
from (
  select date,
         ordinal,
         lag(date)    over (order by ordinal) as prev_date,
         lag(ordinal) over (order by ordinal) as prev_ordinal
  from trading_sessions
) t
where prev_ordinal is not null and ordinal - prev_ordinal <> 1;

-- name: 每个标的每类事件最多只能有一条未来行
--
-- 财报**会被改期**，分红除息日同理。若整窗删+插没做干净，库里会留下一条
-- 已作废的预告行，而 `min(未来 event_date)` 会取到它 —— 倒计时走向一个
-- 不存在的日子，到期后翻成「财报后 1 天」，**播报一场从未发生的财报**。
-- 再过几天数字又自己对了，于是事后更难发现。
--
-- 按 (symbol, event_type) 分组，**两类事件都要查** ——
-- 初版只查 earnings，而分红有完全相同的失败模式。
select symbol || ' ' || event_type || ' has ' || count(*)::text || ' future rows' as violation
from symbol_events
where event_date >= current_date
group by symbol, event_type
having count(*) > 1;

-- name: 期待财报的启用标的必须有未来财报行，或在 10 天内刚报过
--
-- 三个必须容忍的情形（§3.5(5)）：
--   · QQQ 是 ETF，永远没有财报 → 靠 expects_earnings 排除；
--   · 公司周二报完，下一季预告往往要几天后才出现 → 10 天宽限窗口；
--   · 库是空的（M3 刚跑完、M4 还没回填）→ 下面的 exists 守卫。
-- 长期飘红的断言会把 §12 #9 整套补偿策略训练成「反正它总是红的」。
--
-- 条件写成**一个**区间而不是两个 or 分支：`event_date >= current_date` 严格
-- 含于 `>= current_date - 10`，写成两支会读起来像两条独立容差 ——
-- 将来有人收紧宽限那一支（比如加 `and not is_estimated`），
-- 第一支会继续悄悄满足整个谓词，断言就此永远变绿。
select s.symbol as violation
from symbols s
where s.enabled
  and s.expects_earnings
  -- 守卫必须**按标的**，不能是全局的 `exists (select 1 from symbol_events)`：
  -- §3.5(4) 的事件抓取是逐标的限速进行的（private.fetch_state 记录进度），
  -- 所以 M4 的分批回填里会有「第一只已落库、其余十几只还没轮到」的中间态。
  -- 全局守卫在那一刻失效，于是每一只还没抓的标的当场飘红，
  -- 而长期飘红的断言会把整套补偿策略训练成「反正它总是红的」。
  and exists (
    select 1 from private.fetch_state fs
    where fs.symbol = s.symbol and fs.last_event_fetch_at is not null
  )
  and not exists (
    select 1 from symbol_events e
    where e.symbol = s.symbol
      and e.event_type = 'earnings'
      and e.event_date >= current_date - 10
  );

-- name: 历史行的事件列必须全为 NULL
--
-- §3.5(3)：事件距离只在最新一个 session 的行上算。这条防的是
-- 「滚动改写悄悄回来」—— 那会让同一条历史行今天显示 28、明天显示 35。
--
-- **八列都要查。** 初版漏了两个 is_estimated 标记，于是一次「把估计标记
-- 盖到历史行上、但天数留空」的回归会完整通过。
select symbol || ' ' || date::text as violation
from metrics_daily
where date < (select max(date) from metrics_daily)
  and (days_to_next_earnings is not null
       or days_since_last_earnings is not null
       or days_to_next_dividend is not null
       or days_since_last_dividend is not null
       or next_earnings_date is not null
       or next_dividend_date is not null
       or next_earnings_is_estimated is not null
       or next_dividend_is_estimated is not null);

-- name: 最新一天的榜单行数必须等于排名池大小
--
-- **查的是视图，不是基表。** 初版查 strength_daily —— 那样一个
-- 「0002 迁移重建视图时漏了一个分区列」的回归完全抓不到：基表没动，
-- 这条过；「date 都在 trading_sessions 里」过；「in_top_n 必有
-- days_in_top_n」也过（row_number 仍返回非 NULL，只是**算错**）。
-- 首页每张卡片显示一个编造的「在榜 N 天」，而 11 条断言全绿。
--
-- 期望值从**最新行自己带的 rank_pool** 推出，不硬编码 'stock' ——
-- 后者把 strength.yaml 的一个配置决定钉死在 SQL 里，
-- 一旦改成 rank_pool: all，断言会按 ETF 数量永久飘红。
--
-- 守卫放在 having 里而不是 where：无 group by 的聚合查询在零行输入上
-- 仍会产出恰好一个分组，where 里的守卫根本轮不到被求值。
--
-- **期望值要减去「分数算不出来」的那些标的。**
-- §4.1 规则 ③：非有限分（新加入、bar 数不足 min_bars）的标的**不进榜**
-- —— rank_pool 直接把它们丢掉，不是给一个空名次。
-- 不减去它们，这条断言会在**加标的那一天**对一个完全健康的库报警，
-- 而长期飘红的断言会把整套补偿策略训练成「反正它总是红的」。
select 'latest ranking has ' || count(*)::text || ' rows, expected ' || (
         select count(*) from symbols sy
         join metrics_daily m
           on m.symbol = sy.symbol and m.date = max(v.date)
         where sy.enabled
           and (max(v.rank_pool) = 'all' or sy.type = 'stock')
           and m.mom_20 is not null
       )::text as violation
from v_strength_enriched v
where v.date = (select max(date) from v_strength_enriched)
-- 守卫查的是**基表**，不是视图。
--
-- 写成 `count(*) > 0` 时，一个返回 0 行的 v_strength_enriched 让这条通过 ——
-- 而「视图悄悄少返回行」恰恰是 B 段开头那句话点名的失败模式，
-- 也是这条断言之所以改查视图的理由。用基表守卫，空库仍然绿，
-- 而「基表有行、视图没有」立刻红。
having exists (select 1 from strength_daily)
   and count(*) <> (
         select count(*) from symbols sy
         join metrics_daily m
           on m.symbol = sy.symbol and m.date = max(v.date)
         where sy.enabled
           and (max(v.rank_pool) = 'all' or sy.type = 'stock')
           and m.mom_20 is not null
       );

-- name: in_top_n 为真的行必须有 days_in_top_n
select date::text || ' ' || symbol as violation
from v_strength_enriched
where in_top_n and days_in_top_n is null;

-- name: 视图不得比基表少行 —— v_strength_enriched 行数必须等于 strength_daily
--
-- 上面两条都建立在「视图能看见基表的每一行」之上，而它们各自的守卫都会被
-- 一个**返回 0 行的视图**满足。这条把那个前提本身变成断言：它没有守卫、
-- 也不需要 —— 空库上 0 = 0，正常；视图丢行时立刻红，且只此一条会红，
-- 报告因此能直接指向病因而不是症状。
select 'view has ' || (select count(*) from v_strength_enriched)::text
       || ' rows but strength_daily has ' || (select count(*) from strength_daily)::text
       as violation
where (select count(*) from v_strength_enriched) <> (select count(*) from strength_daily);

-- name: 管道不得静默停摆 —— 最新指标日必须紧跟最近一个交易 session
--
-- §7.2.1：「管道不复存在」这一整类故障产出的是**沉默**，在告警设计里
-- 与成功不可区分。这条把它变成一个响亮的断言。
-- 容忍 1 个 session 的滞后（当天收盘后、管道还没跑的那段时间）。
--
-- select 列表里**不要出现聚合**：`m` 和 `ts` 已经各自是单行子查询，再包一层
-- max() 会把整条查询变成「没有 group by 的聚合」，那种查询在零行输入上
-- 依然吐出一行 —— 于是空库（M4 回填之前）立刻假报一次违规，
-- 而下面那两个 is not null 守卫一个字都没起作用。实测撞过一次。
--
-- 空库守卫挂在 **prices_daily** 上，不挂在 metrics_daily 上。
-- 写成 `m.date is not null` 时，一张被清空的 metrics_daily 会让这条
-- 断言永久静音 —— 而那恰恰是「管道不复存在」最成立的时候。
-- prices_daily 是原始事实层、writer 连 DELETE 权限都没有（§8.1.1），
-- 拿它当「这个库已经在用了」的判据比拿一张每天被重写的表可靠。
select 'metrics max=' || coalesce(m.date::text, 'EMPTY')
       || ' but latest session=' || ts.date::text as violation
from (select max(date) as date from metrics_daily) m
cross join (
  select max(date) as date from trading_sessions where date <= current_date
) ts
where ts.date is not null
  and exists (select 1 from prices_daily)
  and (m.date is null
       or (select count(*) from trading_sessions
           where date > m.date and date <= ts.date) > 1);

-- name: 最新行的事件倒计时必须与 symbol_events 一致
--
-- 派生值与事实表脱钩是 §3.5(1) 末尾专门点名的那条。
--
-- 三处都是初版漏掉的，每一处都指向同一类沉默：
--
-- 1. **`e.next_date is null` 必须算违规。** 初版用 `and e.next_date is not null`
--    把它排除了 —— 而「metrics 还在倒数一个 symbol_events 里已经不存在的日子」
--    正是 §3.5(1) 描述的那个失败：倒计时走向一个不存在的日子，到期后翻成
--    「财报后 1 天」，**播报一场从未发生的财报**。被排除掉的恰好是主角。
-- 2. **两类事件都要查。** 分红有完全相同的失败模式（同 §3.5 那条的教训）。
-- 3. **`next_*_date` 也要对。** 天数对而日期错，前端的日期标签就在撒谎。
select m.symbol || ' ' || t.kind || ' says ' || t.stored_days::text
       || ' / ' || coalesce(t.stored_date::text, 'NULL')
       || ' but events say ' || coalesce(e.next_date::text, '没有未来行') as violation
from metrics_daily m
cross join lateral (values
  ('earnings', m.days_to_next_earnings, m.next_earnings_date),
  ('dividend', m.days_to_next_dividend, m.next_dividend_date)
) as t(kind, stored_days, stored_date)
join lateral (
  select min(event_date) as next_date
  from symbol_events se
  where se.symbol = m.symbol
    and se.event_type = t.kind
    and se.event_date > m.date
) e on true
where m.date = (select max(date) from metrics_daily)
  and t.stored_days is not null
  and (e.next_date is null
       or t.stored_days <> (e.next_date - m.date)
       or t.stored_date is distinct from e.next_date);

-- name: 榜单名次必须能由 metrics_daily.mom_20 精确复现
--
-- §3.0 规则 2 要求价格层、指标层、榜单层**三层一起**重写。
-- 只重写前两层时，strength_daily 会停留在旧的名次上，而
-- days_in_top_n / rank_delta_1d 正是从那张表读的 —— 于是它们描述的
-- 是一个已经不再由存储指标导出的排名，且没有任何东西会对账。
select s.date::text || ' ' || s.symbol || ' stored rank=' || s.rank::text
       || ' recomputed=' || r.recomputed::text as violation
from strength_daily s
join lateral (
  select count(*) + 1 as recomputed
  from metrics_daily m2
  join symbols sy on sy.symbol = m2.symbol
  where m2.date = s.date
    and m2.mom_20 is not null
    and sy.enabled
    and (s.rank_pool = 'all' or sy.type = 'stock')
    and (m2.mom_20 > (select mom_20 from metrics_daily
                      where symbol = s.symbol and date = s.date)
         or (m2.mom_20 = (select mom_20 from metrics_daily
                          where symbol = s.symbol and date = s.date)
             and m2.symbol < s.symbol))
) r on true
where s.score_metric = 'mom_20'
  and s.date = (select max(date) from strength_daily)
  -- **只对「自己的分数算得出来」的行做对账。**
  --
  -- §4.1 规则 ①②③：非有限值一律当 -inf、排除出 top-N，但**仍然入榜**
  -- （§4.4 存全部 16 名）。于是一只刚加进 universe.yaml、bar 数还不到
  -- min_bars 的标的，mom_20 是 NULL，排在最后一名。
  -- 而上面那个自查子查询对它返回 NULL → 比较两边都是 NULL → 计数 0
  -- → recomputed = 1，而 stored rank = 16。**一个健康的库当场飘红**，
  -- 就在加标的那一天。
  and exists (
    select 1 from metrics_daily ms
    where ms.symbol = s.symbol and ms.date = s.date and ms.mom_20 is not null
  )
  and s.rank <> r.recomputed;

-- name: 不得有久悬未决的 running 运行记录
--
-- §9.1.2 把「崩溃落在 T2 中间 → runs 里留下一行永远停在 running」称作
-- **这正是我们想要的信号**：下一跑看到它即可判定上一跑硬崩。
--
-- 但那个信号此前**没有任何消费者**：`_already_done` 只匹配 ok / ok_events_stale，
-- 本文件一条 runs 断言都没有，别处也不读 private.runs。
-- 而 daily.yml 有 `timeout-minutes: 30` —— 超时被杀时写下的，
-- 恰恰是那一行没人看的记录。
--
-- 2 小时：整跑按 §7.3.1 的设计是 5–10 分钟，而 workflow 上限是 30 分钟，
-- 取一个宽裕到不可能误报、又短到当天就能发现的数。
select 'run ' || id::text || ' 自 ' || started_at::text || ' 起一直是 running' as violation
from private.runs
where status = 'running'
  and started_at < now() - interval '2 hours';

-- name: ok_events_stale 的那一跑不得发布事件倒计时
--
-- §3.5(4)：事件抓取失败时「核心价格与四个核心指标照常写入，**事件列写 NULL**」。
--
-- 为什么必须单独有这一条：上面那条「倒计时必须与 symbol_events 一致」
-- **抓不到这个错误** —— 它比的正是同一条陈旧的行，于是完全自洽。
-- 而抓取失败的时候，库里那条「下一次财报」恰恰最可能是已经被改期、
-- 已经作废的那一条（改期正是我们每周去抓一次的全部理由）。
-- 拿它发布出去，倒计时会走向一个不存在的日子，到期后翻成「财报后 1 天」。
select m.symbol || ' ' || m.date::text as violation
from metrics_daily m
where m.date = (select max(date) from metrics_daily)
  and exists (
    select 1
    from private.runs r
    where r.session_date = m.date
      and r.status = 'ok_events_stale'
      and r.finished_at = (
        select max(r2.finished_at) from private.runs r2
        where r2.session_date = m.date and r2.finished_at is not null
      )
  )
  and (m.days_to_next_earnings is not null
       or m.days_since_last_earnings is not null
       or m.days_to_next_dividend is not null
       or m.days_since_last_dividend is not null
       or m.next_earnings_date is not null
       or m.next_dividend_date is not null
       or m.next_earnings_is_estimated is not null
       or m.next_dividend_is_estimated is not null);

-- name: 至少一只分红股的某个 yfinance 历史行满足 close != adj_close
--
-- §3.0 规则 4 的探测器：新版 yfinance 默认 auto_adjust=True，此时返回的
-- Close 就是复权价且没有 Adj Close 列 —— 两个列会被写入同一个数字，
-- 「最新价」对任何有分红/拆股历史的股票都对不上券商软件。
-- `check (close > 0)` 这类约束抓不到它：约束必须是关于两者**差值**的。
--
-- 用 `<> 0` 而不是 `> 0.0001`：两列都是 numeric(14,4)，
-- 最小可表示差恰好是 0.0001，用 `>` 会把它排除在外。
-- **窗口限定在最近 400 根 bar 的日历范围内**（§12 #5 的 lookback_bars）。
-- 不限窗时它只要求「历史上曾经有过一行」——而这条断言防的是一次**将来**的
-- yfinance 升级：升级之后每一条**新**行都会 close == adj_close，
-- 而回填期留下的老行会让它永远绿着。要检测的是「现在还在不在发生」。
select 'no recent yfinance row has close != adj_close' as violation
where exists (
    select 1 from prices_daily
    where source = 'yfinance' and date >= current_date - 400
  )
  and not exists (
    select 1 from prices_daily
    where source = 'yfinance'
      and date >= current_date - 400
      and close is not null
      and close <> adj_close
  );
