-- 0002: 月度备份用的只读角色 backup_reader
--
-- ┌────────────────────────────────────────────────────────────────────┐
-- │ 与 0001 同样的前提：角色 backup_reader 必须**已经存在**。          │
-- │ 本文件只做属性与授权，**不含 create role** —— 密码绝不进仓库：     │
-- │     create role backup_reader login password '…';                  │
-- │ 反过来（先跑本文件）下面每一条都会因为角色不存在而失败。           │
-- └────────────────────────────────────────────────────────────────────┘
--
-- **为什么需要这个角色。**
-- §9.4 的月度 `pg_dump` 用 `pipeline_writer` 跑**从来没成功过**（2026-09-21
-- 手动 dispatch 一次 heartbeat 才发现，否则要到 10-01 第一次定时跑才暴露）。
-- 六张表都开了 RLS（0001 的 375–386 行），而 `pipeline_writer` 既不是 owner、
-- 也没有 BYPASSRLS，于是 pg_dump **拒绝**导出：
--
--     pg_dump: error: query would be affected by row-level security policy
--              for table "metrics_daily"
--
-- 那个拒绝是对的：带着 RLS 导出只会得到「这个角色看得见的那些行」——
-- 一份**静默残缺**的备份，而它的持有者以为自己有后悔药。
-- `--enable-row-security` 能让 pg_dump 闭嘴照导，**正因如此不用它**
-- （§10.6「不是白屏，是看起来没事」的数据库版本）。
--
-- **这个角色的边界，逐条说清楚：**
--
-- * 只读 —— 没有任何 insert / update / delete / truncate。
-- * 只在我们自己的两个 schema 上（`public` + `private`），
--   碰不到 Supabase 的 `auth` / `storage` 等托管 schema。
-- * `bypassrls` 只为读：它绕过的是「导出会不完整」这个限制，
--   而不是任何写入约束。
-- * 万一泄露，最坏后果与 `anon` 能读到的公开数据同级 ——
--   差别只是它还能读 `private` 里的运行日志（§9.1 的 runs / fetch_state）。

begin;

-- **只加 bypassrls，别顺手写 `nosuperuser nocreatedb nocreaterole`。**
-- 那三个是 `create role` 的默认值，本来就不用写；而在 Supabase 上写了会直接失败：
--     ERROR: 42501: permission denied to alter role
--     DETAIL: Only roles with the SUPERUSER attribute may alter roles
--             with the SUPERUSER attribute.
-- 因为这里的 `postgres` **不是超级用户**（rolsuper=false），它只是带着
-- rolbypassrls + rolcreaterole。所以它能把 bypassrls 授出去，却碰不了
-- SUPERUSER 那一族属性。（实测踩过，2026-09-21。）
alter role backup_reader bypassrls;

grant usage on schema public, private to backup_reader;
grant select on all tables    in schema public, private to backup_reader;
grant select on all sequences in schema public, private to backup_reader;

-- **以后新增的表 / 序列也要自动带上。**
-- 0001 对 pipeline_writer 没有这么做（那是有意的：写权限要一张张给）。
-- 但备份这个用途要的恰恰是「全都有」—— 漏掉一张表的备份，
-- 比没有备份更危险：它看起来是一份备份。
alter default privileges in schema public, private grant select on tables    to backup_reader;
alter default privileges in schema public, private grant select on sequences to backup_reader;

commit;
