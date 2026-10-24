# PostgreSQL 合成迁移演练

状态：22张旧应用表的结构与值保真演练本地通过；HTTP适配、真实迁移及平台验收尚未完成。

固定DDL位于migrations/postgresql/0001_legacy_baseline.sql，只由显式迁移函数执行，实例启动不执行。候选schema为board_candidate，与探测协议board_app分离。后续正式schema及发布方式需要版本化迁移，不通过改此文件升级已应用数据库。

源结构是本轮全新合成SQLite初始化生成的sqlite_master清单，保存在legacy-sqlite-schema.json；源结构多表、少表、字段或定义变化均拒绝。当前转换保留TEXT日期和JSON字符串；INTEGER转bigint，REAL转double precision，BLOB转bytea；仅auth_users/auth_audit/phone_cards/operations_history的自增id使用identity。所有主键、唯一键、检查、外键及3个显式索引保留。PostgreSQL主键拒绝NULL，比SQLite历史主键语义更严格；实际数据核对及处置需另获授权，不能静默填补。

迁移记录包含版本及DDL的SHA256；事务级advisory锁协调结构迁移，已有版本校验哈希，禁止覆盖。建schema、表和记录在同一事务。runtime_control默认maintenance，restore_generation为uninitialized，不能直接开放业务写入。

pg_synthetic_migration只接收已连接的本轮生成源库；检查路径为当前测试root/data/board.db，目标必须是本轮新建的127.0.0.1集群，核对data_directory和synthetic-only标识。迁移要求所有目标应用表为空。22表在一个事务内按依赖顺序插入，逐字段比较后提交；identity用事务性ALTER SEQUENCE RESTART，避免setval在回滚后残留。

这是类型兼容演练：包括的人员、会话、任务令牌全部是测试生成值。它不是业务恢复工具，不代表真实会话可迁移或采集可以恢复。正式业务恢复仍须遵守restore-contract-v1.json；全量规模校验、受控管理员初始化、应用SQL适配和真实切换工具仍待实现。

loop-0012补充：源读操作使用单个只读事务快照；自引用账号外键设为可延迟，迁移事务延迟至提交检查，子记录先于父记录也保留。悬空父记录在提交时拒绝并整体回滚，未知源表拒绝。11项迁移、7项写协议和8项能力检查同轮通过；evidence/loop-0012/probe-passed.json。

复现：`.venv-release/Scripts/python.exe -B probe_postgresql.py --root .synthetic-tests/<新目录> --migration-checks`。受限令牌无法启动PG时须使用获准的本地执行环境；finally停库。禁止使用现有业务目录或生产连接参数。

loop-0011：22表各1条合成记录，涵盖中文、NULL、空字符串、前导零、超32位整数、二进制。最后一表故意违反CHECK，验证全部22表仍为空；移除测试约束后逐字段成功；重复导入拒绝，维护默认、外键、identity续号、结构版本哈希漂移拒绝均通过。证据evidence/loop-0011/migration-passed.json和source-hashes.json。不是目标规模或整库灾备验收。

## 首次切换策略演练

first_cutover_synthetic不是普通字段保真复制：复制核对后在同一事务撤销auth_sessions/auth_setup/operations_previews，collection_jobs仅清空token并保留历史state，collection_worker心跳和命令失效，collection_settings强制暂停，runtime_control保持maintenance并换新恢复代次。密码哈希、身份、业务和历史保留。逐字段核对与上述明确的安全转换分别记录，不能声称转换后所有字段不变。

loop-0021最后阶段CHECK失败验证外层事务使复制与安全转换全部回滚；成功后旧会话无效、原密码重新登录成功、源文件SHA256不变。证据evidence/loop-0021/cutover-passed.json。与loop-0020普通同库进程重启保留有效会话不同。两者均仅合成，未执行真实迁移。
