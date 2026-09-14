# Usage Panel

一个 Windows 系统托盘小工具，用同一面板查看 **Codex 额度、DeepSeek API 余额和 Codex Radar 模型评分**。

## 功能

- **Codex**：显示官方账户的剩余额度和重置倒计时，只展示实际返回的额度窗口。
- **DeepSeek API**：使用 Claude Code 中已配置的官方 DeepSeek 密钥查询余额，也支持 `DEEPSEEK_API_KEY` 环境变量。
- **模型评分**：展示 Astra、Sol、Terra、Luna 的 `ultra / max / xhigh / high / medium / low` 档位，支持综合智能、软件工程、视觉空间视图。
- **桌面面板**：每 5 分钟更新，可手动刷新、隐藏到托盘、设置置顶。
- **异常处理**：失败时标注旧数据；额度窗口过期后不自行假定额度已经恢复。

评分来自 [Codex Radar](https://codexradar.com/) 的社区基准，并非人的智商。综合分按软件工程和视觉空间的有效题量加权；缺少任一维度时不生成综合分。

## 运行环境

- Windows 10 / 11
- Python 3.12（从源码运行或自行构建时需要）
- 已安装并登录的 Codex CLI
- 已在 Claude Code 中配置官方 DeepSeek API，或已设置 `DEEPSEEK_API_KEY`

工具只做查询，不发起模型对话、不消费重置卡、不修改 Claude Code 的服务配置。

## 从源码启动

在 PowerShell 中执行：

```powershell
git clone https://github.com/viceleeland/usage-panel.git
cd usage-panel
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe source\app.py
```

关闭窗口会隐藏到托盘。单击绿色柱状图标重新显示，右键图标可刷新或退出。`Ctrl+R` 手动刷新，`Esc` 隐藏。默认不开机自启。

## 构建 Windows 可执行文件

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe build.py
```

输出为 `dist/UsagePanel.exe`。它包含 Python 运行依赖，但仍需本机已登录的 Codex CLI，以及对应的 DeepSeek 配置。双击运行后会在程序旁创建 `data` 目录。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s source -p test_providers.py
```

测试覆盖额度窗口与缺失数据、百分比处理、雷达加权算法，以及不会把其他服务的密钥发给 DeepSeek。测试不使用真实账户或网络请求。

## 配置与隐私

- `USAGE_PANEL_CODEX_EXE`：可选，自定义 Codex 可执行文件路径。
- `CLAUDE_CONFIG_DIR`：可选，Claude Code 配置目录。
- `DEEPSEEK_API_KEY`：可选，优先使用此环境变量提供的 DeepSeek 密钥。不要将实际密钥提交到仓库。
- `data/`：本机额度缓存、余额、评分及面板设置，已从 Git 排除。

DeepSeek 密钥只发送到官方 `api.deepseek.com/user/balance`，不会发送到 Codex Radar。工具不持久化密钥。公开仓库不包含个人用量、余额截图、运行缓存或预编译程序。

## 来源与限制

- [Codex App Server](https://learn.chatgpt.com/docs/app-server)：`account/rateLimits/read`。
- [DeepSeek 余额接口](https://api-docs.deepseek.com/zh-cn/api/get-user-balance/)：账户余额，不是当天消费统计。
- [Codex Radar](https://codexradar.com/)：公开评测数据接口。网站改版可能需要更新适配器。

进度条显示**剩余**比例。系统进程数不等于活动 AI 任务数。综合智能的两项来源可能具有不同更新时间，面板分别标出。

更多操作说明见 [使用说明](使用说明.md)。
