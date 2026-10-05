# Codex × DeepSeek Harness

通过本地 MCP，让 Codex 在你**明确要求时**把工作交给 DeepSeek Harness，在当前聊天展示交接、进度、结果和验收。委派在 Harness 的原生工作区和对话中执行，你可以直接打开原对话接手。

这是独立的第三方桥接项目，使用公开 MCP / Cordis 扩展接口。不会修改客户端二进制或使用 OpenAI 登录凭据，也不会增加 Codex 额度。规划、沟通和验收仍由 Codex 完成并消耗其额度；DeepSeek 走你自己的提供方账户。

## 已支持的功能

- 明确调用：默认仍由 Codex 工作；讨论 DeepSeek 或额度不会自动派发。
- Harness 原生会话、工作区创建和同会话续接，无需再开一个面板。
- 增量进度、最终报告、逐项验收、取消、断线恢复说明。
- 固定验收条件和范围；同一任务最多两轮，故障重试与返工共用上限。
- 用户接手与空闲会话交回；用户接手后 Codex 不再发送或取消任务。
- 提供方报告的 token、美元等值估算、返工合计；可选 Go/Go Plus 套餐占用比例。
- 按需查看完整公开协作时间线，不导入模型私有思考。

## 兼容性与前提

首个公开版本是 **Windows 桌面预览版**，来源于 Windows + DeepSeek Harness **0.2.0-rc.2** 的实际集成。公开安装流程做了隔离测试；其他系统、其他 Harness 版本尚未验证。Harness 的扩展 API 仍可能变化。

需要 Python **3.11+**、已安装并至少启动过一次的 Codex 和 DeepSeek Harness。当前模型路由固定为 **`opencode-go / deepseek-v4.1-flash`**；先在 Harness 自行配置并选择该模型。本项目不提供账号或 API key，不支持自动换提供方。只读和工作区写入依靠 Harness 的原生 Windows 沙箱；委派始终不会选择完全权限。

运行时 Python 仅用标准库，不需要 pip 安装；Node.js 仅用于开发测试，插件使用 Harness 自带 Node 运行时。

## 安装

在 PowerShell 中克隆到一个固定目录，后续移动目录需要更新配置：

```powershell
git clone https://github.com/luoqi1024/codex-deepseek.git
cd codex-deepseek
python scripts/install.py
```

第一步只在 `generated/` 生成配置预览，**不修改客户端配置、不启动程序或调用模型**。查看 `harness.patch.yml`、`codex-mcp.toml`、`SKILL.md`，确认目录和 Python 路径正确。

确认后安装：

```powershell
python scripts/install.py --apply
```

安装器会：

1. 给 `%USERPROFILE%\.dsh\profiles\desktop\cordis.patch.yml` 追加自己的标记配置，并复制连接器入口。保留原有模型、账号、权限预设和默认设置，不重新序列化现有 YAML/`!!js`。
2. 在 `%USERPROFILE%\.codex\config.toml` 追加 `[mcp_servers.deepseek]`；如果设置了 `CODEX_HOME`，使用该目录。
3. 安装 `deepseek-delegate` 技能，设置 `allow_implicit_invocation: false`。
4. 给私有连接目录设置当前 Windows 用户、SYSTEM、Administrators 的访问权限。

已存在的文件先创建带时间戳的备份。现有 `deepseek` MCP、同名技能或未受本安装器管理的连接器会让安装停止，**不会覆盖已有集成**。安装中写入失败会还原已修改文件；私有目录和新建的空目录/备份可能保留。安装器不是自动升级工具。

自定义目录时：

```powershell
python scripts/install.py --profile 'C:\path\to\desktop-profile' --codex-home 'C:\path\to\codex-home'
```

完成后，**彻底退出并重开 Harness，再重启 Codex**。安装器不会代你关闭或打开客户端。在 Codex 中说“检查 DeepSeek 连接”，`deepseek_connection` 应返回在线、连接器 revision 6、`usage_tracking: true`。这只是连接检查，不代表模型鉴权或真实用量已经验证。

Codex 标准 MCP 配置格式参见 [官方 MCP 文档](https://developers.openai.com/codex/mcp/)。如果你已有同名集成，可以对照生成的配置手动迁移，保留自己的备份。

## 日常使用

Harness 保持运行，可以最小化或放在托盘。示例请求：

> 这次用 DeepSeek 子 agent，读取项目说明，指出安装步骤中存在的具体问题；不要修改文件。

Codex 先说明目标、范围和验收条件，再派发；当前聊天展示关键进度，完成后核对证据。当前桥接按模型提交步骤回报进度，不是逐字直播，也不是 Codex 自带子 agent 的原生 UI。

编辑任务必须明确选择 `workspace-write`，调查用 `read-only`。默认只读不会自动扩大权限。重启或断连后使用原任务 ID 检查恢复说明，不能因本地监控退出就认为 Harness 已停止、再次重发任务。

想接手可在 Codex 说“把这条任务交给我”，或直接在 Harness 的 `[Codex] …` 对话发消息。显式 handoff 会先停止委派；直接输入使用 Harness 自己的消息队列语义。也可用 `/codex-takeover` 先停止后接手。原权限保留。

用户接手后 Codex 不再向该会话发消息或取消工作。想交回时明确要求 Codex 交回，或在 Harness 使用 `/codex-return`；原会话和重叠目录必须空闲、无待发消息，交回本身不会执行模型任务。

要看完整日志可明确要求详情；`deepseek_details` 按需启动 `http://127.0.0.1:47831/`，不会自动打开浏览器。普通使用无需详细页面。

## Token、价格与额度

默认 `settings.json` 的 `usage_plan` 为 `null`，只估算美元等值。使用对应套餐时可改为 `"go"` 或 `"go-plus"` 来显示本任务占套餐额度的估算比例。

计数来自公开 `assistant/message`、`assistant/attempt` 和实际 LLM 压缩事件；输入是**未缓存输入**，缓存读取另外计算，reasoning 是输出中的子集，不重复加价。失败重试已报告的用量会纳入，固定任务链两轮合计在 `assignment_total`。

缺失的旧记录显示未知，部分报告明确标注部分，不按字符数猜 token。不含标题生成、嵌套子会话、用户接手后调用；执行中的调用尚未回报时也不在合计内。统计不是账户完整账单。

折算使用 **2026-10-05 的价格快照**：[OpenCode Go 官方价格](https://opencode.ai/docs/go/)。当前 DeepSeek V4.1 Flash 非峰每百万未缓存输入/输出/缓存读取是 $0.15/$0.60/$0.003，高峰两倍；UTC 工作日 01–04、06–10 为高峰。调用跨峰谷边界或开始时间未知时显示区间。Go 该模型月等值额度 $60，Go Plus $120，五小时占月额度 20%，周占 50%。价格和促销可能变化，需核对 `usage_meter.PRICE` 后更新。

美元等值**不是额外订阅扣款**。本任务额度占用比例**不是账户剩余额度**，也不能转换为 Codex Plus 百分比或声称确定的节省比例。

## 配置与权限

| 位置 | 用途 |
|---|---|
| `settings.json` | 默认 `backend: desktop` 和可选套餐类型 |
| Harness desktop profile | 插件入口及本机仓库/日志路径 |
| Codex `config.toml` | Python stdio MCP 启动配置 |
| `%LOCALAPPDATA%\CodexDeepSeek\desktop` | 私有连接发现和任务映射 |
| `runs/任务ID/` | 本机任务指令、公开事件、结果与验收 |

只部署一个活动连接器。它仅监听 loopback 临时端口，以随机本机 capability token 限定 health/submit/status/cancel/handoff/return，拒绝浏览器 Origin。它不读取模型账号凭据；任务和必要文件内容由 Harness 发送给你配置的提供方。

公开仓库不含运行记录、连接令牌、备份、账号配置或第三方客户端源码。你运行后生成的任务日志可能包含项目文件和模型输出，**不要提交到 GitHub**；脱敏规则只覆盖常见形式，不保证去掉所有敏感信息。

高级排障可显式选择 legacy headless 模式，并通过环境变量 `CODEX_DEEPSEEK_DSH` 或 PATH 指定 `dsh.cmd`。还需要自己准备同名 `codex-deepseek` Harness profile；安装器只安装原生桌面模式。没有原生对话展示，不自动作为断线后备。

## 开发与验证

```powershell
python -X utf8 -m unittest test_bridge test_mcp test_desktop test_policy test_usage test_usage_mcp test_install
node --test test_native.mjs test-dashboard.cjs
```

测试使用临时目录、模拟 Harness 服务和本机 loopback，不调用真实模型，不修改你的客户端配置。仓库的 Windows CI 运行相同检查。公开版的隔离安装测试不等于已在另一台全新电脑完成真实客户端联调；上游升级后请先检查连接和沙箱，再在明确授权下做小型任务。

## 卸载

退出 Harness 和 Codex 后，移除 profile 中 `BEGIN/END codex-deepseek managed connector` 整段及 `codex-deepseek-connector.mjs`，移除 Codex 的 `[mcp_servers.deepseek]` 表和安装的同名技能，再重开客户端。也可对照安装前备份恢复；如果之后修改过配置，不要整文件回滚覆盖后来的设置。任务日志和私有映射按需自己保留或删除。

## 许可证与上游

本项目自有代码采用 [MIT](LICENSE)。[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 和 Codex 是各自独立的产品；此仓库不打包客户端、模型或第三方运行时，不代表官方认可，也不保证上游兼容。
