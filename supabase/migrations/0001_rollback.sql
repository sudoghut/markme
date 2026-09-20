-- 0001_init.sql 的回滚。
--
-- AGENTS.md / §9.4：**没有回滚脚本的迁移不允许执行。**
-- 因为不建 staging（§12 #9），生产上唯一的安全网就是「改完马上验、错了能退」。
--
-- 注意它**不**删除 pipeline_writer 角色：那个角色由前提步骤手工创建，
-- 不属于本迁移的产物。删掉它会连带影响任何别的授权。

begin;

-- 视图先于它依赖的表
drop view if exists v_strength_enriched;

-- 触发器随表一起 drop，但函数不会 —— 单独清掉，否则重跑 0001 会撞
-- "function already exists"。
drop table if exists private.fetch_state;
drop table if exists private.runs;
drop schema if exists private;

drop table if exists strength_daily;
drop table if exists metrics_daily;
drop table if exists prices_daily;
drop table if exists symbol_events;
drop table if exists symbols;
drop table if exists trading_sessions;

drop function if exists touch_updated_at();
drop function if exists touch_computed_at();

-- 把 §4.1 收掉的默认权限还原，否则回滚之后库的状态与迁移前不一致 ——
-- 一个「回滚了但没完全回滚」的库比没回滚更难排查。
alter default privileges for role postgres in schema public
  grant all on tables to anon, authenticated;
alter default privileges for role postgres in schema public
  grant all on sequences to anon, authenticated;
alter default privileges for role postgres in schema public
  grant all on functions to anon, authenticated;

-- 0001 给 pipeline_writer 的 schema USAGE 也要收回：那是本迁移的产物。
revoke usage on schema public from pipeline_writer;

-- **anon 的 `grant usage on schema public` 故意不收。**
-- Supabase 建项目时就给过它，0001 里那一句只是把「不取决于别人的默认值」
-- 写出来（见 0001 §4.2）。这里收掉会让库比迁移前**更差**，
-- 前端全站 42501 —— 一个「回滚过头」的回滚比没回滚更难排查。
--
-- 0001 的 `revoke all on all sequences in schema public` 同理不还原：
-- 它是对当时存在的序列的一次快照操作，而那些序列随表一起 drop 了。

notify pgrst, 'reload schema';

commit;
