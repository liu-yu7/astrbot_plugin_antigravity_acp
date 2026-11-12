# Antigravity ACP for AstrBot

[![Tests](https://github.com/liu-yu7/astrbot_plugin_antigravity_acp/actions/workflows/tests.yml/badge.svg)](https://github.com/liu-yu7/astrbot_plugin_antigravity_acp/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

通过**本机官方 Antigravity CLI（`agy acp`）**，将账号可用的模型接入 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的服务提供商体系。

插件通过 ACP（Agent Client Protocol）驱动官方 CLI，不使用第三方模型转发网关。支持流式回复、思考流、图片、多轮会话和 AstrBot 函数工具 / MCP 工具。

> **默认配置是 512K，但不是安装后自动解锁。** 512K / 900K 必须有已应用且通过检查的本地上下文补丁。如果没有补丁，请先在插件配置中将窗口改为 **256K**。插件不会下载脚本或自动修改 agy 二进制。

## 功能

- **一级服务提供商**：在 AstrBot 中添加 Antigravity 提供商源和模型，沿用现有对话流程。
- **流式回复与思考流**：将 ACP 通知映射为 AstrBot 回复片段。
- **图片与多轮历史**：复用 ACP 会话；历史被改写、压缩或重置后，按 AstrBot 历史重建。
- **工具桥接**：通过回环 HTTP MCP 服务挂起工具调用，交由 AstrBot 执行并回填结果。
- **权限隔离**：预置无内置终端 / 文件工具的 `astrbot-bridge` agent，拒绝不属于当前 AstrBot 会话的工具执行请求。
- **可配置上下文**：Default / 256K / 512K / 900K，自动同步本适配器的模型上下文上限。
- **补丁状态检查**：检查失败时明确报错，不把未验证的大窗口当作成功。
- **管理员指令**：登录、状态、模型列表、会话重置和进程重启。

## 环境与兼容性

| 项目 | 要求 / 已验证环境 |
|---|---|
| AstrBot | 已验证 **4.29.4**；其他版本尚未确认 |
| Antigravity CLI | 代码要求 `agy >= 1.4.12`；真实调用及上下文补丁检查已验证 **1.5.5** |
| Python | 语法要求 **3.10+**；真实运行已验证 **3.12** |
| 操作系统 | 完整运行及集成测试已验证 **Linux arm64**；其他平台完整运行尚未确认 |
| Python 依赖 | `aiohttp`，通常由 AstrBot 环境提供；见 [requirements.txt](requirements.txt) |
| 登录 | 拥有 Antigravity 访问权限的账号，完成官方 CLI 登录 |

模型列表、思考档位、上下文能力及额度由上游服务和账号决定。文档与测试中的模型名只是示例，不代表每个账号都能使用。

## 安装与快速开始

### 1. 安装官方 CLI

按照 [Antigravity 官方安装说明](https://antigravity.google/docs/install?tab=cli) 安装，然后确认：

```bash
agy --version
```

CLI 必须与 AstrBot 运行在同一台机器或同一个容器内，且运行 AstrBot 的系统用户能够执行它。在需要代理的环境中，后续给提供商源配置 `proxy`。

### 2. 安装本插件

在 AstrBot WebUI 的插件页选择**从链接安装**，填写：

```text
https://github.com/liu-yu7/astrbot_plugin_antigravity_acp
```

也可以从 [Releases](https://github.com/liu-yu7/astrbot_plugin_antigravity_acp/releases) 下载插件 ZIP 安装，或手动克隆到 AstrBot 插件目录：

```bash
git clone https://github.com/liu-yu7/astrbot_plugin_antigravity_acp.git \
  <AstrBot根目录>/data/plugins/astrbot_plugin_antigravity_acp
```

手动安装后重启 AstrBot。不要把本仓库的父目录、运行数据或个人登录文件打包进插件。

### 3. 选择上下文窗口

WebUI → **插件 → Antigravity (agy acp) → 齿轮配置**：

- **没有上下文补丁**：将 `context_window` 改为 **256K**，保留回放预算 `0`。
- **需要默认 512K**：先按 [上下文说明](docs/CONTEXT.md) 审阅并应用兼容的社区补丁，设置本地检查脚本路径，再保留 **512K**。

保存会热重载插件，可能中断正在生成的回复。

### 4. 添加提供商源与模型

WebUI → **模型提供商 → 新增提供商源**，选择 **Antigravity (agy acp)**。

按需设置 `agy_path` 和 `proxy`。若尚未登录，可先保存提供商源，再执行下一步。登录完成后读取模型列表，添加账号可用模型，并选择默认对话模型。

| 字段 | 默认 | 说明 |
|---|---|---|
| `agy_path` | 空 | agy 的绝对路径；空则按平台默认位置及 PATH 查找 |
| `agy_home` | 空 | 隔离目录；默认 `<AstrBot>/data/plugin_data/astrbot_plugin_antigravity_acp` |
| `proxy` | 空 | 官方 CLI 进程的 HTTP 代理，例如 `http://127.0.0.1:7890` |
| `agy_thought_level` | 空 | 默认思考档位；具体可选值取决于模型。模型 ID 中的档位后缀优先 |
| `agy_expose_tools` | 开 | 是否向模型暴露当前 AstrBot 工具 |
| `agy_max_sessions` | 6 | 会话容量目标；每个活跃 ACP 会话可能使用独立 worker |
| `agy_idle_ttl` | 900 | 空闲会话回收时间，单位秒 |
| `timeout` | 300 | 等待模型下一个输出的超时，单位秒 |

`NO_PROXY` 会排除回环地址，确保本机 MCP 桥不经过代理。并发上限优先回收空闲会话，不会为达到目标而中断正在执行的会话。

例如 `gemini-3.8-flash-high` 可映射为基础模型加思考档位；以 `/agy models` 或提供商的模型列表为准。模型条目的能力按实际使用设置为 `text`、`image`、`tool_use`。

### 5. 登录并验证

`/agy` 指令只允许 **AstrBot 管理员** 使用。若提示权限不足，发送 `/sid`，将发送者 ID 加入 WebUI 的**管理员 ID**配置；WebUI 聊天的发送者 ID 是该 WebUI 用户名。

在可信私聊或 WebUI 聊天中发送：

```text
/agy login
```

打开返回的 Google 授权链接，完成登录，再提交授权码：

```text
/agy code <授权码>
/agy status
/agy models
```

也可以用运行 AstrBot 的系统用户在终端执行 `agy acp --login`。Linux SSH / systemd 环境如无法访问桌面凭据服务，可使用：

```bash
DBUS_SESSION_BUS_ADDRESS=unix:path=/nonexistent agy acp --login
```

需要代理时为终端命令添加 `HTTPS_PROXY` / `HTTP_PROXY`。不要在群聊、Issue 或公开日志中发送授权码、token 或密码。

## 插件配置

| 字段 | 默认 | 说明 |
|---|---|---|
| `context_window` | `512K` | Default / 256K / 512K / 900K；K 按 1000 计 |
| `replay_budget_bytes` | `0`（自动） | 完整补丁下按窗口 tokens × 2，最多 2,200,000 字节；512K 为 1,024,000 字节。可设置更小的正整数 |
| `patch_script` | 空 | 可信本机 `agy-patch.py` 的绝对路径；留空查找 `~/.gemini/antigravity/tools/agy-patch.py` |

保存后，插件会停止原有 agy 进程，同步本适配器模型的 `max_context_tokens`，并在新进程启动时写入两个隔离 settings 文件。不修改其他提供商或终端用户自己的 agy 配置。

**字节回放预算不等于 token 数。** 它用于重建会话时选择保留哪些文本历史，不保证完整保留所有消息，也不扩大上游模型的实际能力。详见 [上下文与补丁](docs/CONTEXT.md)。

## 管理指令

```text
/agy status          查看版本、登录态、窗口、回放预算、补丁状态及进程
/agy login           开始官方 Google 登录
/agy code <授权码>    提交浏览器授权码
/agy models          刷新当前账号可用模型列表
/agy reset           丢弃 ACP 会话，下次按 AstrBot 历史重建
/agy restart         停止 agy，下次请求按当前配置启动
```

## 隐私与安全边界

- 模型请求发往官方 Antigravity 服务，不经本项目维护的转发服务器；**对话、图片、系统提示和工具结果仍会发送给上游服务**。
- OAuth 交换和模型认证由官方 CLI 完成。插件不解析 token 内容，也不会通过自建 HTTP 客户端重放 token；但为供隔离 CLI 使用，**会将官方凭据文件复制到插件私有目录**，并在可用的平台上设置为 `0600`。
- `astrbot-bridge` 去掉内置终端和文件工具；这不是操作系统沙箱。暴露的 AstrBot 工具仍拥有各自权限，应只启用可信工具。
- `patch_script` 是会在 AstrBot 用户权限下执行的本地 Python 程序。`--dry-run` 不是对恶意脚本的沙箱，只能配置自己审阅并信任的脚本。
- ACP 日志、AstrBot 聊天历史、授权码消息和运行配置可能包含敏感数据。提交日志前必须脱敏，**不要上传运行数据目录**。

本项目不是 Google / Antigravity 或 AstrBot 的官方插件，也不改变账号套餐、配额或服务条款。

## 排障与开发

- 权限、登录、窗口和代理问题：见 [排障指南](docs/TROUBLESHOOTING.md)。
- 架构、测试和辅助工具：见 [开发说明](docs/DEVELOPMENT.md)。
- 版本变更：见 [CHANGELOG.md](CHANGELOG.md)。
- 安全问题与私密报告：见 [SECURITY.md](SECURITY.md)。

自动测试无需 Google 登录，不调用真实模型。真实 Gemini 短请求和 WebUI 窗口切换已在上述 Linux 环境验证；**没有进行 512K 满载测试**，也不承诺长上下文质量或所有平台兼容。

## 许可与致谢

采用 [MIT License](LICENSE)。参考并适配了 [lili-luo/dsh-plugin-bundle](https://github.com/lili-luo/dsh-plugin-bundle) 中的 `dsh-llm-antigravity-acp` 的 ACP、历史重建和 MCP 工具桥接思路，保留其版权和许可声明，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

官方 agy 二进制、登录凭据和社区二进制补丁脚本**均不随本插件分发**。
