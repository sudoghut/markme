-- 0004: 利率政策板块（docs/add-rate-policy.md，M16）
--
-- 四张表：
--   rate_policy_daily   联邦基金目标区间 + EFFR，按生效日一行；`change_*` 标出加息/降息那一天
--   treasury_yields     国债收益率，(date, tenor) 一行
--   fomc_meetings       FOMC 决议会议日程（整窗删+插，代理主键 —— 改期不会留下孤儿行）
--   private.macro_fetch_state   每个来源最近一次成功 / 尝试 / 失败原因
--
-- **对旧代码是纯增量的**：不改任何已有的表、列、函数或约束。
--
-- ┌─ 部署顺序**不是**硬性的（与 0003 相反）────────────────────────────────┐
-- │ 前端读这些表是**软失败**的：表缺失 / 读失败 / 表为空，窄条都只是显示   │
-- │ 「暂不可用」或整条隐藏，页面其余部分不受影响。                         │
-- │                                                                        │
-- │ 但 invariants.sql 在 keepalive 里连库跑，它会查这四张表 ——             │
-- │ **迁移没跑就合并，那一跑会红**（「断言本身执行失败」算违规，这是有意的）。│
-- │ 所以建议顺序：① 本文件 → ② 合并 PR → ③ 手动触发一次 macro workflow 回填。│
-- │ 回滚则先撤代码，再跑 0004_rollback.sql。                               │
-- └────────────────────────────────────────────────────────────────────────┘
--
-- 本文件**不是幂等的**（没有 if not exists）：重复执行会在事务里报错并整体回滚，
-- 是安全的失败。执行者是 postgres（与 0001 相同），不含 create role。

begin;

-- ---------------------------------------------------------------------------
-- 1. 表
-- ---------------------------------------------------------------------------

-- 单位一律是**百分数**（3.875 = 3.875%）。存的和页面展示的是同一个单位，
-- 不在中间换算 —— 差一个 /100 看起来就是「市场崩盘」（create-project.md §3.4）。
create table rate_policy_daily (
  date         date primary key,
  target_lower numeric(6,3) not null,
  target_upper numeric(6,3) not null,
  effr         numeric(6,3) not null,
  -- 目标区间相对**前一行**变了的那一天才有值，其余为 NULL。判定只在管道里做一次，
  -- 前端不重新实现。mixed = 两沿变了但不同向或不同幅：**不猜方向**，bp 留空。
  change_kind  text,
  change_bp    smallint,
  source       text not null default 'nyfed',
  updated_at   timestamptz not null default now(),
  -- 下沿可以是 0：2022-03-16 之前的零利率时期，目标区间是 0.00–0.25。
  constraint rate_policy_range_ck  check (target_lower >= 0 and target_lower < target_upper
                                          and target_upper < 25),
  constraint rate_policy_effr_ck   check (effr > 0 and effr < 25),
  constraint rate_policy_change_ck check (
       (change_kind is null   and change_bp is null)
    or (change_kind = 'up'    and change_bp > 0)
    or (change_kind = 'down'  and change_bp < 0)
    or (change_kind = 'mixed' and change_bp is null))
);

create table treasury_yields (
  date       date not null,
  tenor      text not null,
  yield_pct  numeric(6,3) not null check (yield_pct > 0 and yield_pct < 25),
  updated_at timestamptz not null default now(),
  primary key (date, tenor)
);

-- 主键是**代理主键**，不是日期：会议被改期时，日期主键会让旧的那一行变成孤儿
-- （create-project.md §3.5(1) 的同一个教训，美联储 2026-11-12 刚改过 2027 年日程）。
-- 幂等性来自**整窗删+插**，不来自主键冲突。
create table fomc_meetings (
  id         bigint generated always as identity primary key,
  start_date date not null,
  end_date   date not null,
  fetched_at timestamptz not null default now(),
  check (end_date >= start_date and end_date - start_date <= 2)
);
create index fomc_meetings_end_idx on fomc_meetings (end_date);

create table private.macro_fetch_state (
  source          text primary key check (source in ('nyfed', 'treasury', 'fomc')),
  last_ok_at      timestamptz,
  last_attempt_at timestamptz not null,
  last_error      text
);

-- updated_at 由触发器维护（0001 的 touch_updated_at），不靠写入方自觉。
create trigger t_rate_policy_touch before update on rate_policy_daily
  for each row execute function touch_updated_at();
create trigger t_treasury_touch before update on treasury_yields
  for each row execute function touch_updated_at();

-- ---------------------------------------------------------------------------
-- 2. 访问控制：与 0001 §4 / §5 同一套 —— GRANT 与 RLS 是两道
-- ---------------------------------------------------------------------------

-- 先收权（Supabase 的隐式默认权限会给 anon 全部权限），再显式给读。
revoke all on rate_policy_daily, treasury_yields, fomc_meetings from anon, authenticated;
grant select on rate_policy_daily, treasury_yields, fomc_meetings to anon;

alter table rate_policy_daily enable row level security;
alter table treasury_yields   enable row level security;
alter table fomc_meetings     enable row level security;

create policy "public read" on rate_policy_daily for select to anon using (true);
create policy "public read" on treasury_yields   for select to anon using (true);
create policy "public read" on fomc_meetings     for select to anon using (true);

-- 写入角色：**恰好需要的动词**。
--   · rate_policy_daily / treasury_yields：整窗 upsert，**不给 DELETE** ——
--     它们是原始事实层，写入凭证被攻陷时不该有能力抹掉它们。
--   · fomc_meetings：整窗删+插，所以要 DELETE，**不要 UPDATE**（没有就地改的路径）。
--     日程可从官方页面完整再生，删得起。
-- 不给序列任何授权：identity 列的 nextval 不查调用者对序列的 USAGE（见 0001 §5 的实测）。
grant select, insert, update on rate_policy_daily, treasury_yields to pipeline_writer;
grant select, insert, delete on fomc_meetings                      to pipeline_writer;
grant select, insert, update on private.macro_fetch_state          to pipeline_writer;

create policy "writer sel" on rate_policy_daily for select to pipeline_writer using (true);
create policy "writer ins" on rate_policy_daily for insert to pipeline_writer with check (true);
create policy "writer upd" on rate_policy_daily for update to pipeline_writer using (true);

create policy "writer sel" on treasury_yields for select to pipeline_writer using (true);
create policy "writer ins" on treasury_yields for insert to pipeline_writer with check (true);
create policy "writer upd" on treasury_yields for update to pipeline_writer using (true);

create policy "writer sel" on fomc_meetings for select to pipeline_writer using (true);
create policy "writer ins" on fomc_meetings for insert to pipeline_writer with check (true);
-- delete 用 using (true)：**不要写成受限谓词** —— 那会让整窗删+插静默删不完，
-- 反而留下孤儿行（与 0001 对 symbol_events 的说明相同）。
create policy "writer del" on fomc_meetings for delete to pipeline_writer using (true);

-- PostgREST 的 schema 缓存不会自己发现新建的对象。
notify pgrst, 'reload schema';

commit;
