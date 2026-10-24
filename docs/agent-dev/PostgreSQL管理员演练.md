# PostgreSQL 无图形管理员演练

状态：仅合成测试工具本地通过；正式配置、最小权限平台账号和生产操作流程未接通，不构成生产初始化授权。

pg_admin.py要求显式synthetic-root与当前探测环境一致，目标由connect_synthetic核对127.0.0.1、集群目录及synthetic-only标识。不接受生产DSN，不读取默认配置，不自动建结构。

initialize-owner和reset-owner均要求审查过的迁移版本、maintenance及共享控制行锁。初始化只允许22张应用表全空，已有owner拒绝覆盖。重置必须精确指定当前owner用户名，保留身份/角色/其他人员/业务资料，撤销owner所有会话和setup凭据；不会开放维护状态。受控操作的operator、reason及动作进入auth_audit；密码和哈希不进入输出或审计。

密码默认getpass输入，或从受保护管道以--password-stdin读取一行；没有密码命令行参数。不在命令行/代码包/日志中放真实凭据。本轮测试只用生成密码，通过调用同一CLI main及替换stdin演练，没有读用户输入或真实秘密。

复现整套演练：`.venv-release/Scripts/python.exe -B probe_postgresql.py --root .synthetic-tests/<新目录> --admin-checks`。

loop-0018检查：空环境初始化、重复初始化拒绝、维护前提、用户名精确匹配、owner会话撤销而员工会话保留、业务保留、新密码登录/旧密码失败、审计留痕无秘密、维护保留。证据evidence/loop-0018/admin-passed.json，source_stable=true、server_stopped=true。

尚缺：通过正式受控配置启动工具。loop-0256确认合成库唯一登录角色是超级用户，不能当作最小权限；服务仍使用该角色。loop-0257证明一个不能登录的受限角色不能建库、不能建角色，服务连接没有更换。loop-0258列出运行时权限：查看数据目录对受限角色被拒绝，环境标记读取也被拒绝，服务连接没有更换。平台最小权限账号未接通。首次真实迁移不保留旧会话的独立策略，以及与web_server进程启动的集成演练，仍未做。不能把本合成工具用作客户库迁移命令。
