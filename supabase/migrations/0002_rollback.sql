-- 0002 的回滚。§9.4：**没有回滚脚本的迁移不允许执行。**
--
-- 顺序与 0002 相反：先撤未来的默认授权，再撤已有对象的授权，最后撤角色属性。
-- 反过来做的话，撤完 usage 之后那几条 revoke 仍然能跑（revoke 不需要 usage），
-- 但读起来会像「先锁门再收钥匙」——顺序写对，下一个人才不用重新推一遍。

begin;

alter default privileges in schema public, private revoke select on tables    from backup_reader;
alter default privileges in schema public, private revoke select on sequences from backup_reader;

revoke select on all sequences in schema public, private from backup_reader;
revoke select on all tables    in schema public, private from backup_reader;
revoke usage  on schema public, private from backup_reader;

alter role backup_reader nobypassrls;

commit;

-- **角色本身不在这里删。** 它的创建（含密码）就在仓库外，删除也一样：
--     drop role backup_reader;
-- 删之前记得把 GitHub 的 MARKME_BACKUP_URL secret 一起删掉，
-- 否则留下的是一条指向不存在角色的连接串 —— heartbeat 会红，
-- 而红的原因看上去像是网络问题。
