-- 0004 的回滚：撤掉利率政策板块（docs/add-rate-policy.md，M16）。
--
-- **先撤代码，再跑本文件。** 反过来的话，macro workflow 与 invariants.sql 会撞上
-- 不存在的表。前端是软失败的，页面本身不会挂。
--
-- 触发器随表一起 drop；`touch_updated_at()` 是 0001 的函数，**不在这里动它**。

begin;

drop table if exists private.macro_fetch_state;
drop table if exists fomc_meetings;
drop table if exists treasury_yields;
drop table if exists rate_policy_daily;

notify pgrst, 'reload schema';

commit;
