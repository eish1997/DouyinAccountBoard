# PostgreSQL 写入准入协议 v1（本地组件）

状态：PostgreSQL16.15真实多连接验证通过，尚未接入现有app.py业务接口，不构成A15/A16/A18/A21整体验收。

## 控制结构

应用自有schema为board_app。runtime_control仅一行，主键id=1。

| 字段 | 类型/约束 | 语义 |
| --- | --- | --- |
| mode | text，maintenance或open | 默认迁移创建时maintenance；人工核验后才open |
| writer_epoch | bigint，非空 | 当前获准写入的发布代次；旧实例不匹配拒绝 |
| restore_generation | text，非空 | 不透明恢复代次；生产使用随机UUID字符串；旧页面/请求不匹配拒绝 |

当前测试DDL只为探测准备；正式结构由后续受控迁移建立，不由实例启动自动创建。

## 顺序和边界

pg_write_protocol.write_transaction必须在空闲连接上进入新事务。首先FOR UPDATE锁定runtime_control，检查维护、实例代次及请求恢复代次，然后才允许调用方在同一事务重新读取人员、会话、权限和实体范围、检查实体版本、写入业务及审计。锁保持到提交或回滚。
锁顺序固定为控制行 → 人员授权行 → 按实体键排序的实体/版本行 → 幂等唯一键。撤权/维护管理事务也先锁控制行，不能绕过它先修改权限。
独立协议组件尚未实现会话、办公室或字段权限，调用方不能把获得控制锁当作业务授权。
首版以此串行准入保证现有全表审计不会混入其他事务提交；性能是否足够必须按固定负载验证，不做无证据承诺。

写事务已获准时，后来的撤权等待其完成；撤权先提交时，等待的写事务必须在获得控制锁后读到新授权并拒绝。
维护转换持同一锁，等待此前事务结束后持久化maintenance，新写请求获得锁后拒绝；排空超时必须保持停写控制流程，后续维护工具还需实现超时状态，不得假设组件自动完成所有恢复协调。
组件目前lock_timeout=5秒，statement_timeout=15秒，用于本地协议验证；大导入/导出等完整请求的预算尚需按计划和平台限制分类实现，不将此值当作全系统最终限制。

## 错误

TRANSACTION_ALREADY_ACTIVE、CONTROL_STATE_MISSING、MAINTENANCE、WRITER_VERSION_RETIRED、RESTORE_GENERATION_EXPIRED均拒绝进入业务写入。
数据库锁超时/不可达仍由驱动抛出，HTTP稳定错误码映射、请求编号和结果未知处理属于T04/T08未完成项。

## 证据

evidence/loop-0010/protocol-passed.json：同步屏障CAS单胜者及审计归属、观察pg_stat_activity锁等待后撤权拒写、反向顺序已准入写先完成、旧实例/恢复代次拒写、维护状态跨连接生效。
使用合成protocol_user/protocol_record/protocol_audit表；这没有代替真实业务表、关联记录、auth_sessions和办公室权限集成验证。

运行：项目专用`.venv-release/Scripts/python.exe -B probe_postgresql.py --root .synthetic-tests/<新目录> --protocol-checks`。
Windows当前沙箱的受限令牌不能启动postgres，需要获准的本机测试执行环境；脚本只使用本任务新cluster和127.0.0.1，finally停库。禁止改参数连接其他数据库。
