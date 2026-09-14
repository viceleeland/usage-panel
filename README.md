<div align="center">

![Usage Panel — Your AI, at a glance](docs/assets/hero.svg)

### 额度 · 余额 · 模型评分，一个托盘面板看清。

让 **Codex、DeepSeek API 和 Codex Radar** 待在桌面一角，随时查看。

[![Tests](https://github.com/viceleeland/usage-panel/actions/workflows/tests.yml/badge.svg)](https://github.com/viceleeland/usage-panel/actions/workflows/tests.yml)
![Windows](https://img.shields.io/badge/Windows-10%20%2F%2011-173d2b?style=flat-square)
![Python](https://img.shields.io/badge/Python-3.12-173d2b?style=flat-square&logo=python&logoColor=b8f3a5)
![Version](https://img.shields.io/badge/version-1.0.0-8ce7a2?style=flat-square&labelColor=173d2b)

[快速开始](#quick-start) · [功能一览](#features) · [构建程序](#build) · [使用说明](使用说明.md)

<sub>横幅为功能示意，不展示真实账户数据。</sub>

</div>

---

<a id="features"></a>

## 🟢 小面板，三种视角

| **01 / CODEX** | **02 / DEEPSEEK** | **03 / MODEL RADAR** |
| :--- | :--- | :--- |
| **还剩多少额度？** | **API 账户还有多少余额？** | **各档位表现如何？** |
| 官方账户额度、剩余进度条与重置倒计时。 | 读取 Claude Code 中的 DeepSeek API 配置，显示官方余额。 | Astra、Sol、Terra、Luna，按推理档位对照社区评测。 |

### 日常使用，保持轻巧

- **每 5 分钟自动刷新**，也可以随时手动查询。
- **关闭即隐藏到托盘**，支持面板置顶。
- **旧数据明确标记**，查询失败不会伪装成新结果。
- **只读查询**，不发起模型对话，不消耗重置卡，不切换你的服务配置。

> [!NOTE]
> 进度条显示的是**剩余比例**。账户未返回的额度窗口不会显示，也不会被当成 0% 或 100%。

<a id="quick-start"></a>

## ⚡ 快速开始

准备 **Windows 10 / 11 + Python 3.12**，在 PowerShell 中运行：

```powershell
git clone https://github.com/viceleeland/usage-panel.git
cd usage-panel
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe source\app.py
```

**连接你的数据：**

| 数据 | 准备工作 |
| :--- | :--- |
| Codex | 本机安装 Codex CLI，并完成登录。 |
| DeepSeek | Claude Code 已配置官方 DeepSeek API，或设置 `DEEPSEEK_API_KEY` 环境变量。 |
| Codex Radar | 无需账户，自动读取公开评测数据。 |

### 随手就能用

| 操作 | 效果 |
| :--- | :--- |
| `Ctrl + R` | 立即刷新 |
| `Esc` / 关闭窗口 | 隐藏到托盘，继续自动更新 |
| 单击绿色托盘图标 | 重新显示面板 |
| 右键托盘图标 | 刷新或退出 |
| 勾选「置顶」 | 面板保持在其他窗口上方 |

<a id="build"></a>

## 📦 打包成独立程序

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe build.py
```

生成 **`dist/UsagePanel.exe`**，双击即可运行，无需另装 Python。

程序仍需读取本机 Codex 登录及 DeepSeek 配置；运行后在旁边创建 `data/` 文件夹。默认不开机自启。当前仓库提供源码与构建脚本，未发布预编译下载包。

## 🧠 Astra 与模型评分

**六个档位，一张表：**

`ultra` → `max` → `xhigh` → `high` → `medium` → `low`

| 视图 | 含义 |
| :--- | :--- |
| **综合智能** | 软件工程与视觉空间两个维度，按有效题量加权。 |
| **软件工程** | 软件工程基准成绩。 |
| **视觉空间** | 视觉空间推理基准成绩。 |

> [!IMPORTANT]
> IQ 来自 [Codex Radar](https://codexradar.com/) 的**社区基准评分**，并非人的智商，也不是模型厂商的官方评级。缺失档位显示 `—`；任一维度缺失时，不生成综合分。两组来源可能不同步，面板分别标出更新时间。

## 🔐 数据留在哪里？

**登录交给原有工具，面板只读取所需信息。**

- DeepSeek 密钥只发送到官方 `api.deepseek.com/user/balance`，不会发送给 Codex Radar。
- 本工具不持久化 API 密钥，也不修改 Claude Code 的服务配置。
- 额度、余额、评分缓存和窗口设置存放于本机 `data/`，已从 Git 排除。
- 公开仓库不包含个人用量、余额截图或运行缓存。

<details>
<summary><strong>展开高级配置</strong></summary>

| 环境变量 | 用途 |
| :--- | :--- |
| `USAGE_PANEL_CODEX_EXE` | 指定非标准位置的 Codex 可执行文件。 |
| `CLAUDE_CONFIG_DIR` | 指定 Claude Code 配置目录。 |
| `DEEPSEEK_API_KEY` | 优先使用此变量中的 DeepSeek 密钥。请勿提交真实密钥。 |

</details>

<details>
<summary><strong>展开测试与项目结构</strong></summary>

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s source -p test_providers.py
```

测试无需真实账户或网络请求，覆盖额度窗口、缺失数据、百分比处理、雷达加权算法及密钥发送目标。

```text
usage-panel/
├── source/
│   ├── app.py               # 托盘与面板
│   ├── providers.py         # 额度、余额、评分适配器
│   └── test_providers.py    # 无凭据单元测试
├── docs/assets/             # README 视觉资源
├── .github/workflows/       # 自动测试
├── build.py                 # Windows 打包入口
└── 使用说明.md
```

</details>

## 📡 数据来源

| 来源 | 读取内容 |
| :--- | :--- |
| [Codex App Server](https://learn.chatgpt.com/docs/app-server) | `account/rateLimits/read` 官方账户额度 |
| [DeepSeek API](https://api-docs.deepseek.com/zh-cn/api/get-user-balance/) | 账户余额，非当天消费统计 |
| [Codex Radar](https://codexradar.com/) | 公开模型评测数据，网站改版时可能需要更新适配器 |

系统进程数不等于活动 AI 任务数。更多操作细节见 [使用说明](使用说明.md)。

---

<div align="center">

**少切几个窗口，多看一眼全局。**

[报告问题](https://github.com/viceleeland/usage-panel/issues) · [查看更新](CHANGELOG.md) · [回到顶部](#readme)

</div>
