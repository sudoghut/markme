-- 0003 的回滚。§9.4：**没有回滚脚本的迁移不允许执行。**
--
-- 顺序与 0003 相反：先撤触发器，再收窄状态枚举，最后删列。
--
-- **收窄枚举之前，库里不能还有 `ok_preliminary` 的行**，否则 add constraint 失败、
-- 整个回滚被事务撤回（这是好事：它不会留下半回滚的库）。要回滚就先决定那些行
-- 记成什么，例如：
--     update private.runs set status = 'ok' where status = 'ok_preliminary';
-- 同理，删列会丢掉「哪些行是临时值」这条信息 —— 回滚前先看一眼：
--     select date, count(*) from prices_daily where preliminary group by 1;

begin;

drop trigger t_prices_keep_final on prices_daily;
drop function keep_final_prices();

alter table private.runs drop constraint runs_status_check;
alter table private.runs add constraint runs_status_check check (status in (
  'running','ok','skipped_holiday','skipped_too_early',
  'skipped_already_done','ok_events_stale',
  'stale_vendor','partial','failed'));

alter table prices_daily drop column preliminary;

commit;

notify pgrst, 'reload schema';
