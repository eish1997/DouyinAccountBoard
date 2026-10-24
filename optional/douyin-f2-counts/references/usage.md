# 安装与使用

## 分享给其他人

解压整个 `douyin-f2-counts` 文件夹。Codex 用户可把它放入自己的 `~/.codex/skills/`，重新打开会话后使用 `$douyin-f2-counts`。其他支持 SKILL.md 的工具按各自的 skill 安装位置放置。没有 Agent 也可以直接运行脚本。

仅分享 skill 文件夹或 ZIP。`profile` 浏览器目录、虚拟环境和运行输出不属于分享包；首次登录须在接收者自己的电脑上完成。

## 创建独立运行环境

需要 Python 3.11+；本次实际测试使用 Python 3.14。以下命令在解压后的 skill 目录执行。

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe scripts/douyin_counts.py login --profile ./private-profile
```

macOS / Linux（配套脚本可移植，但本次未实机验证这两个系统）：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python scripts/douyin_counts.py login --profile ./private-profile
```

脚本打开独立 Chromium 窗口。在其中正常登录抖音；必要时手动完成验证，之后回到终端按回车。登录状态只存于指定目录。登录和查询不能同时占用同一个目录。

## 批量查询

复制 `assets/accounts.example.json` 为自己的账号列表并修改。支持 JSON 数组（抖音号字符串、含 `account` 的对象、主页字符串，或含 `url` 与可选 `account` 的对象），也支持 TXT 每行一个抖音号或完整主页。直接按抖音号示例：`["97350513058"]`，无需提供主页。查询时程序会正常搜索并严格核对真实抖音号；`--headed` 可以显示独立搜索窗口。可选 `account` 用于验证返回的抖音号；请用字符串保留前导零。

```powershell
.\.venv\Scripts\python.exe scripts/douyin_counts.py query --profile ./private-profile --input ./assets/accounts.example.json --output ./results
```

macOS / Linux 将上面的 Python 路径替换为 `.venv/bin/python`。使用自己安装的兼容 Chromium 时，可在 `login` 或 `query` 后增加 `--browser-executable "完整浏览器程序路径"`；默认使用 Playwright 安装的 Chromium。

每次运行创建独立的时间目录，含：

- `results.html`：可在浏览器打开，点击昵称访问抖音主页。
- `results.csv`：可用 Excel 打开，失败的数量为空。
- `results.json`：原始统计结果、状态、北京时间和 F2 来源。

默认顺序请求、间隔 3 秒，连续失败 3 次停止余下请求。可用 `--interval 5 --timeout 20 --max-failures 3` 调整。失败仍会保存已获得的结果。退出码：0 全部成功，2 有失败或跳过，1 输入或启动错误。

## 排查

- `LoginRequired`：重新执行 login，不需要向 Agent 粘贴 Cookie。
- `APIRetryExhaustedError`：常见于 200 空正文，不能据此认定账号不存在。先检查同一独立窗口是否可以正常访问账号，再重新登录或检查上游变更。
- `IdentityMismatch`：返回标识与输入不一致，检查 `account` 是否确为当前抖音号。
- `ProfileUnavailable`：资料字段缺失、账号私密或接口未正常返回，不写成 0。
- 浏览器未安装：对同一个虚拟环境执行 `python -m playwright install chromium`。
- 网络不通：检查自己的正常网络及代理设置。脚本保留环境代理配置，不自动修改系统代理。

不要在请求持续失败时无限循环、轮换账号或自动解验证码。本次已验证的是会话有效时的小批量读取；不能把它解释为抖音接口长期不变或无需维护。

按抖音号搜索若返回 SearchNeedsVerification，请手动完成独立窗口验证后再重试；SearchUnavailable 只表示未取得唯一精确匹配，不代表账号不存在。登录和查询不能同时占用同一个独立会话目录。

2026-10-20排查补充：HTTP200、status_code=0也可能携带search_nil_info.search_nil_type=verify_check，此时必须按安全验证处理，不能视为查无账号。query增加--interactive选项，会保留独立窗口等待人工验证，回复后继续从当前页精确提取身份。验证完成不会自动重试整个账号列表。

本机2026-10-20验证：accounts.json仅["97350513058"]，已登录并曾手动处理搜索验证后，query --headed一次自动完成搜索与数量查询，风笛道路救援1032作品；报告E:/Downloads/DouyinF2Test-20261020/results/20261020-022651-a1f67f/results.html。此路径为该测试机证据，不是分享包所含文件；未接工作台或验证大批量长期运行。
