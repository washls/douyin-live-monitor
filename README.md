<p align="center">
  <img src="https://img.shields.io/badge/python-3.9+-blue.svg" alt="Python">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License">
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg" alt="Platform">
  <img src="https://img.shields.io/badge/version-1.2.0-orange.svg" alt="Version 1.2.0">
  <img src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg" alt="PRs Welcome">
</p>

<p align="center">
  <h1 align="center">📺 抖音直播监听器</h1>
  <p align="center">监控指定抖音博主的直播状态，开播时通过 <b>Server 酱³</b> 推送通知到手机</p>
</p>

---

## ✨ 功能特性

- 👥 **多主播监控**：在一个进程中同时监控多个主播，各自维护直播与通知状态
- 🔍 **直播状态监控**：定时检查已启用主播，自动识别开播、持续直播、切换直播间和下播
- 🔄 **8 种检测方式**：Douyin API、Webcast、用户主页和 IES 接口自动回退
- 📲 **完整通知流程**：支持开播、下播、持续直播、启动测试和每日亲密度提醒
- 🛡️ **通知防重**：推送超时或连接异常时不自动重放同一个 POST，避免服务端已接收后再次发送
- ⏰ **持续直播提醒**：默认每 10 分钟提醒一次，间隔和最多提醒次数都可配置
- 🌙 **每日亲密度提醒**：主播在线时，在 23:57、23:58、23:59 分别发送提醒
- 🧠 **记住上次主播**：再次启动时可继续监控，也可以切换主播
- 🪶 **低资源占用**：默认最多并发检测 2 个主播，复用检测连接并限制连接池和页面缓存
- 📝 **滚动日志**：终端只显示状态摘要，详细日志自动轮转，避免长期运行无限增长
- ⌨️ **随时退出**：输入 `q` 后回车，或按 `Ctrl+C`，即可停止监控并退出
- 🎯 **交互式配置**：首次运行按提示填写推送 URL 和主播链接
- 📦 **Windows EXE**：Releases 提供打包好的可执行文件，无需安装 Python

## 🆕 v1.2.0

这一版增加了多主播监控。每个主播使用独立的检测客户端、通知器和状态，单个任务连续失败不会中断其他主播。旧版记录的单主播会自动加入新列表，原状态文件不会被删除。如需回退，可以继续使用 [v1.1.1](https://github.com/washls/douyin-live-monitor/releases/tag/v1.1.1)。

## 🚀 快速开始

### Windows 直接运行

从 [Releases](https://github.com/washls/douyin-live-monitor/releases) 下载 `douyin-monitor-v1.2.0.exe`，双击后按照提示完成配置。

### 从源码运行

#### 环境要求

- **Python 3.9+**

#### 安装

```bash
# 克隆仓库
git clone https://github.com/washls/douyin-live-monitor.git
cd douyin-live-monitor

# 安装依赖
pip install -r requirements.txt

# Windows 用户也可以运行安装脚本
setup.bat
```

#### 运行

```bash
# 首次运行：跟随引导完成配置
python monitor.py

# 测试推送连接
python monitor.py --test

# 单次检测（调试用）
python monitor.py --once

# 持续监控（详细日志）
python monitor.py -v

# 使用自定义配置
python monitor.py --config my_config.json

# 查看版本
python monitor.py --version

# 静默运行，仅在出错时输出
python monitor.py --quiet

# 保存原始响应，排查检测问题
python monitor.py --debug

# 查看主播列表
python monitor.py --list-streamers

# 添加主播，可选保存显示名称
python monitor.py --add-streamer "https://www.douyin.com/user/xxxxx" --label "主播名称"

# 停用或重新启用主播
python monitor.py --disable-streamer STREAMER_ID
python monitor.py --enable-streamer STREAMER_ID

# 删除主播，脚本或无输入环境需要加 --yes
python monitor.py --remove-streamer STREAMER_ID
```

持续监控会启动全部已启用的主播。输入 `q` 并回车即可停止所有任务，也可以按 `Ctrl+C`。

### 首次运行引导

程序首次启动时会**自动弹出配置引导**：

1. **配置推送**：粘贴你的 Server 酱³ 推送 URL（从 [sc3.ft07.com](https://sc3.ft07.com) 获取）
2. **选择主播**：粘贴第一个要监控的抖音博主主页链接
3. **开始监控**：配置自动保存，下次启动会监控列表中所有已启用主播

> 升级时会把 `.monitor_state.json` 中原有的单主播加入新列表，并保留旧状态文件。其他主播可以通过 `--add-streamer` 添加。

## 👥 主播管理

每个主播都有一个 12 位本地 ID，单个配置最多保存 100 个主播。使用 `--list-streamers` 查看 ID，再执行停用、启用或删除操作。删除需要确认，非交互环境必须同时使用 `--yes`。

```bash
python monitor.py --list-streamers
python monitor.py --add-streamer "https://www.douyin.com/user/xxxxx" --label "显示名称"
python monitor.py --disable-streamer STREAMER_ID
python monitor.py --enable-streamer STREAMER_ID
python monitor.py --remove-streamer STREAMER_ID --yes
```

同一抖音账号如果通过两个不同链接重复添加，启动时会根据解析出的 `sec_uid` 识别，保留列表中靠前的任务并暂停重复项。

## ⚙️ 配置说明

`config.json` 由程序自动生成和管理，也可以手动编辑：

```json
{
    "push_url": "",
    "sendkey": "",
    "push_uid": "",
    "check_interval": 30,
    "notify_on_stream_end": true,
    "repeat_notify_interval": 600,
    "max_repeat_notifications": 3,
    "startup_notify": false,
    "enable_daily_intimacy_reminder": true,
    "max_concurrent_checks": 2,
    "streamers": [
        {
            "id": "a1b2c3d4e5f6",
            "url": "https://www.douyin.com/user/xxxxx",
            "label": "主播名称",
            "enabled": true
        }
    ]
}
```

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `push_url` | Server 酱³ 完整推送 URL | 首次运行时引导填写 |
| `sendkey` | Server 酱³ SendKey（由 URL 自动解析） | 空 |
| `push_uid` | Server 酱³ 用户 UID（由 URL 自动解析） | 空 |
| `check_interval` | 检测间隔（秒） | 30 |
| `notify_on_stream_end` | 是否推送下播通知 | true |
| `repeat_notify_interval` | 持续直播重复提醒间隔（秒） | 600 |
| `max_repeat_notifications` | 单次直播最多重复提醒次数 | 3 |
| `startup_notify` | 启动时发送测试通知 | false |
| `enable_daily_intimacy_reminder` | 23:57 至 23:59 发送亲密度提醒 | true |
| `max_concurrent_checks` | 同时检测的主播数量，范围为 1 至 8 | 2 |
| `streamers` | 主播列表，由程序或管理命令维护，最多 100 项 | 空列表 |

> **获取推送 URL**：访问 [sc3.ft07.com](https://sc3.ft07.com) → 微信扫码登录 → 「发送消息」→ 复制完整推送 URL

> **隐私说明**：`config.json`、`*.log`、`.monitor_state.json`、`debug_dumps/`、`dist/`、`build/` 和原子写入产生的临时文件均不会提交到 Git 仓库。请勿公开包含真实推送 URL、SendKey 或运行日志的文件。

## 🔔 通知规则

| 场景 | 默认行为 |
|------|----------|
| 检测到开播 | 立即发送一次开播通知 |
| 持续直播 | 首次通知成功后，每 10 分钟提醒一次，最多 3 次 |
| 切换直播间 | 房间 ID 变化后视为新一轮直播，重新发送开播通知 |
| 检测到下播 | 发送下播通知，可通过配置关闭 |
| 每日亲密度提醒 | 主播在线时，在 23:57、23:58、23:59 各提醒一次 |
| 推送超时或连接异常 | 不自动重复提交相同 POST，避免服务端已接收时产生重复通知 |
| 单个主播连续检测失败 | 达到 10 次后只暂停该主播，其他任务继续运行 |

## 🖥️ 运行控制与日志

- 输入 `q` 后回车，或按 `Ctrl+C`，可立即结束等待并退出程序。
- `monitor.log` 每个文件最大 2 MB，最多保留当前文件和 2 个备份，总占用约 6 MB。
- `--verbose` 在终端显示详细日志，`--quiet` 只显示错误。
- `--debug` 会把原始接口响应保存到 `debug_dumps/`。这些文件可能含主播标识或接口参数，排查完成后请妥善处理。

## 🔬 检测原理

程序内置 **8 种检测方法**，按优先级自动回退：

| 优先级 | 方法 | 说明 |
|:---:|------|------|
| 1 | Douyin API | 调用抖音 `aweme/v1/web/user/profile/other` 接口（最可靠） |
| 2 | Webcast info_by_user | `live.douyin.com/webcast/room/info_by_user/` |
| 3 | Profile 直播链接 | 从用户主页提取直播间链接 |
| 4 | HTML 页面解析 | 解析 RENDER_DATA、__INITIAL_STATE__、__pace_f 等内嵌数据 |
| 5 | IES API v2 | 调用 `iesdouyin.com/web/api/v2/user/info/` |
| 6 | Webcast API | `live.douyin.com/webcast/user/info/` |
| 7 | IES 分享页 | 解析 `iesdouyin.com/share/user/` 页面 |
| 8 | 直播间页面 | 直接访问直播间 URL 检测 |

## 📬 通知示例

开播时收到的微信推送：

```markdown
📺 开播提醒

- 博主: 某某主播
- 标题: 今晚的直播标题
- 房间ID: 123456789
- 链接: 点击观看

⏰ 检测时间: 2025-06-30 20:30:00
```

## 📁 项目结构

```
douyin-live-monitor/
├── monitor.py           # 主程序入口 & 交互式引导
├── monitor_service.py   # 多主播调度、状态汇总与统一停止
├── streamer_config.py   # 主播列表校验、迁移与原子保存
├── douyin_client.py     # 抖音直播状态检测核心（8 种方法）
├── notifier.py          # Server 酱³ 推送通知模块
├── abogus.py            # a_bogus / msToken 签名生成 (纯 Python)
├── x-bogus.js           # PyInstaller 打包所需签名资源
├── test_monitor.py      # 单主播状态与推送回归测试
├── test_monitor_service.py  # 多主播调度回归测试
├── test_streamer_config.py  # 配置迁移与管理回归测试
├── douyin-monitor.spec  # PyInstaller 打包配置
├── config.example.json  # 配置文件示例
├── requirements.txt     # Python 依赖
├── setup.bat            # Windows 一键安装脚本
├── LICENSE              # MIT 开源协议
└── README.md
```

## ❓ 常见问题

<details>
<summary><b>推送失败，提示"客户端错误"？</b></summary>
请检查推送 URL 是否正确。访问 <a href="https://sc3.ft07.com">sc3.ft07.com</a> 重新获取。
</details>

<details>
<summary><b>检测不到直播状态？</b></summary>
抖音接口和页面结构会不定期变化。程序会从 Douyin API 开始检测，并自动尝试其余方法。可以先运行 <code>python monitor.py --once --verbose</code> 查看本次尝试的方法；需要保留原始响应时再使用 <code>--debug</code>。
</details>

<details>
<summary><b>如何让程序开机自启？</b></summary>

**Windows**：创建任务计划程序（Task Scheduler）

```powershell
# 触发器: 开机时启动
# 操作: 运行 python monitor.py（需完整路径）
# 或直接运行打包好的 douyin-monitor.exe
```

**Linux**：使用 systemd service
```ini
[Unit]
Description=Douyin Live Monitor
[Service]
ExecStart=/usr/bin/python3 /path/to/monitor.py
Restart=always
[Install]
WantedBy=multi-user.target
```
</details>

<details>
<summary><b>检测频率应该设多快？</b></summary>
建议 ≥ 30 秒。过于频繁可能触发抖音风控，导致 IP 被临时限制。
</details>

<details>
<summary><b>打包好的 exe 在哪里？</b></summary>
在 <a href="https://github.com/washls/douyin-live-monitor/releases">Releases</a> 页面下载最新版本。
</details>

<details>
<summary><b>如何回退到上一个版本？</b></summary>
从 <a href="https://github.com/washls/douyin-live-monitor/releases/tag/v1.1.1">v1.1.1 Release</a> 重新下载旧版 EXE。v1.2.0 的迁移不会删除旧的 <code>.monitor_state.json</code>，旧版仍可读取原来的单主播记录。
</details>

## ⚠️ 免责声明

- 本工具仅供**学习研究**使用
- 请遵守抖音平台的**服务条款**
- 请勿将本项目用于**商业用途**或其他违反平台规定的用途
- 使用者需自行承担使用风险

## 🙏 依赖的开源项目

本项目基于以下优秀的开源项目构建：

| 项目 | 用途 | 协议 |
|------|------|------|
| [DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder) | a_bogus / msToken 签名算法 | GPL-3.0 |
| [Server 酱³](https://sc3.ft07.com) | 微信消息推送服务 | 未注明 |
| [requests](https://github.com/psf/requests) | Python HTTP 客户端库 | Apache-2.0 |
| [PyInstaller](https://github.com/pyinstaller/pyinstaller) | Python 打包工具 | GPL-2.0 |

特别感谢 [@ihmily](https://github.com/ihmily) 的 DouyinLiveRecorder 项目提供的签名算法分析。

## 📄 协议

本项目采用 [MIT License](LICENSE) 开源协议。

---

<p align="center">
  <sub>Made with ❤️ by the open source community</sub>
</p>
