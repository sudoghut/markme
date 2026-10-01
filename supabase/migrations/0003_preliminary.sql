-- 0003: 收盘后先出临时结果，次日定稿（docs/provisional-close.md，M13）
--
-- 三件事：
--
-- 1. `prices_daily.preliminary` —— 这一行是不是临时值（收盘后的分钟线拼出来的，
--    或日线还在成形中）。**历史行必须是 false**：前一天及更早的数字不再变。
-- 2. `private.runs.status` 多一个取值 `ok_preliminary`（exit 0，但**不算**「做完了」，
--    于是当晚 / 次日的定稿跑不会被条件重试跳过）。
-- 3. 触发器：**定稿行永远不会被临时行覆盖。** 放在库里而不是 upsert 语句里，
--    因为触发器扛得住「有人忘了写」—— 与 0001 的 touch_updated_at 同一个理由。
--
-- **对旧代码是纯增量的**：不写这一列的写入一律落成 false（= 定稿），
-- 与 0003 之前的语义完全相同；新状态值旧代码不会产出、旧触发器不受影响。
--
-- ┌─ 部署顺序是硬性的：**先跑本迁移，再合并 M13 的代码** ────────────────┐
-- │ 反过来（代码先上）：前端查询多了 `preliminary` 这一列 → PostgREST 400   │
-- │ → `failClosed` → **每个访客都拿到 500**，而 `/api/health` 不读这一列、  │
-- │ 照样 200；管道每一跑 failed；keepalive 的 invariants 因为一条断言报错  │
-- │ 而连锁刷出一串假违规（闸门 A 第 1 轮 S1）。                              │
-- │ 顺序：① 本文件 → ② `invariants.sql` → ③ 下面的「执行后验证」→          │
-- │ ④ 集成测试 → ⑤ 才合并 PR。回滚则反过来：先撤代码，再跑 0003_rollback。 │
-- └────────────────────────────────────────────────────────────────────────┘
--
-- 执行时机：`alter table` 会对 prices_daily / private.runs 加 ACCESS EXCLUSIVE 锁，
-- 避开 daily 正在跑的时段（执行前看一眼 Actions）。本文件**不是幂等的**
-- （没有 if not exists）：重复执行会在事务里报错并整体回滚，是安全的失败。
--
-- ── 执行后验证（以 owner 身份在 SQL Editor 里跑，最后 rollback，不留痕迹）──
--   begin;
--   -- 取任意一行定稿行，试图把它改成临时值：应当**整行跳过**，一个字都不变
--   with victim as (select symbol, date, close from prices_daily
--                   where not preliminary order by date desc limit 1)
--   update prices_daily p set preliminary = true, close = p.close + 1
--   from victim v where p.symbol = v.symbol and p.date = v.date
--   returning p.symbol;            -- 期望：0 行
--   rollback;

begin;

alter table prices_daily
  add column preliminary boolean not null default false;

comment on column prices_daily.preliminary is
  '临时值（收盘后的分钟线 / 尚在成形的日线）。定稿后为 false；历史行必须是 false。';

alter table private.runs drop constraint runs_status_check;
alter table private.runs add constraint runs_status_check check (status in (
  'running','ok','skipped_holiday','skipped_too_early',
  'skipped_already_done','ok_events_stale','ok_preliminary',
  'stale_vendor','partial','failed'));

-- 定稿 → 临时：**整行跳过**（return null），不是报错。
--
-- 报错会让整个 T2 回滚 —— 一只标的的分钟线回退，就把其余 16 只当天的定稿一起丢了。
-- 而跳过的结果恰好就是正确答案：库里留着定稿值。管道不会主动这么写
-- （当天只在定稿时刻之前才打临时标，过去的日子只保留、不新造临时行），
-- 这一条是给「供应商的日线又消失了、分钟线顶上」这类意外留的底。
-- `set search_path = ''`：函数体只碰 old / new，不引用任何对象；钉死它免得 Supabase
-- Security Advisor 报 function_search_path_mutable（0001 的 touch_* 有同样的老告警）。
create function keep_final_prices() returns trigger language plpgsql
set search_path = '' as $$
begin
  if not old.preliminary and new.preliminary then
    return null;
  end if;
  return new;
end
$$;

-- 触发器按名字字母序执行：`t_prices_keep_final` 在 `t_prices_touch` 之前，
-- 于是被跳过的行连 updated_at 都不会被碰 —— 它确实没有被更新。
create trigger t_prices_keep_final before update on prices_daily
  for each row execute function keep_final_prices();

commit;

notify pgrst, 'reload schema';
