<p align="center">
  <img src="https://img.shields.io/badge/python-3.9+-blue.svg" alt="Python">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License">
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg" alt="Platform">
  <img src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg" alt="PRs Welcome">
</p>

<p align="center">
  <h1 align="center">📺 抖音直播监听器</h1>
  <p align="center">监控指定抖音博主的直播状态，开播时通过 <b>Server酱³</b> 推送通知到手机</p>
</p>

---

## ✨ 功能特性

- 🔍 **自动检测** — 监控指定抖音博主是否开播
- 📲 **实时推送** — 开播/下播时通过 Server酱³ 推送通知到微信
- 🔄 **多层回退** — 6 种检测方法自动切换，确保可靠性
- 📝 **完整日志** — 控制台 + 文件双通道日志
- 🪶 **轻量无依赖** — 无需浏览器，纯 HTTP 请求
- 🎯 **交互式配置** — 首次运行引导式配置，无需手动编辑配置文件
- 📦 **即开即用** — 提供打包好的 Windows exe

## 🚀 快速开始

### 环境要求

- **Python 3.9+**
- Node.js *(可选，用于 API 签名生成，没有也能正常运行)*

### 安装

```bash
# 克隆仓库
git clone https://github.com/YOUR_USERNAME/douyin-live-monitor.git
cd douyin-live-monitor

# 安装依赖
pip install -r requirements.txt

# Windows 用户可直接双击运行
setup.bat
```

### 运行

```bash
# 首次运行 — 跟随引导完成配置
python monitor.py

# 测试推送连接
python monitor.py --test

# 单次检测（调试用）
python monitor.py --once

# 持续监控（详细日志）
python monitor.py -v

# 使用自定义配置
python monitor.py --config my_config.json
```

### 首次运行引导

程序首次启动时会**自动弹出配置引导**：

1. **配置推送** — 粘贴你的 Server酱³ 推送 URL（从 [sc3.ft07.com](https://sc3.ft07.com) 获取）
2. **选择主播** — 粘贴要监控的抖音博主主页链接
3. **开始监控** — 配置自动保存，下次启动直接进入监控

> 之后再次运行时，会记住上次监控的主播，可选择继续或切换。

## ⚙️ 配置说明

`config.json` 由程序自动生成和管理，也可以手动编辑：

```json
{
    "push_url": "",
    "sendkey": "",
    "push_uid": "",
    "check_interval": 30,
    "notify_on_stream_end": true,
    "retry_times": 3,
    "retry_delay": 5
}
```

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `push_url` | Server酱³ 完整推送 URL | 首次运行时引导填写 |
| `sendkey` | Server酱³ SendKey（由 URL 自动解析） | — |
| `push_uid` | Server酱³ 用户 UID（由 URL 自动解析） | — |
| `check_interval` | 检测间隔（秒） | 30 |
| `notify_on_stream_end` | 是否推送下播通知 | true |
| `retry_times` | 推送失败重试次数 | 3 |
| `retry_delay` | 重试间隔（秒） | 5 |

> **获取推送 URL**：访问 [sc3.ft07.com](https://sc3.ft07.com) → 微信扫码登录 → 「发送消息」→ 复制完整推送 URL

## 🔬 检测原理

程序内置 **6 种检测方法**，按优先级自动回退：

| 优先级 | 方法 | 说明 |
|:---:|------|------|
| 1 | HTML 页面解析 | 解析用户主页内嵌的 `RENDER_DATA` 数据（**最可靠**） |
| 2 | IES 分享页解析 | 解析 `iesdouyin.com` 分享页面 |
| 3 | IES API v2 | 调用抖音用户信息 API v2 |
| 4 | Douyin API | 调用抖音 `aweme/v1/web/user/profile/other` 接口 |
| 5 | Webcast 房间检测 | 通过直播广场 WebSocket 接口查询 |
| 6 | 直播间页面 | 直接访问直播间 URL 检测 |

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
├── douyin_client.py     # 抖音直播状态检测核心
├── notifier.py          # Server酱³ 推送通知模块
├── abogus.py            # a_bogus / msToken 签名生成 (纯 Python)
├── x-bogus.js           # 签名生成 (Node.js，可选)
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
抖音的反爬机制会定期更新。程序默认使用 HTML 页面解析（最稳定）。如失效请检查日志，或尝试更换 Cookie。
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
在 <a href="https://github.com/YOUR_USERNAME/douyin-live-monitor/releases">Releases</a> 页面下载最新版本。
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
| [Server酱³](https://sc3.ft07.com) | 微信消息推送服务 | — |
| [requests](https://github.com/psf/requests) | Python HTTP 客户端库 | Apache-2.0 |
| [PyInstaller](https://github.com/pyinstaller/pyinstaller) | Python 打包工具 | GPL-2.0 |

特别感谢 [@ihmily](https://github.com/ihmily) 的 DouyinLiveRecorder 项目提供的签名算法分析。

## 📄 协议

本项目采用 [MIT License](LICENSE) 开源协议。

---

<p align="center">
  <sub>Made with ❤️ by the open source community</sub>
</p>
