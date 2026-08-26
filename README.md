<p align="center">
  <img src="https://img.shields.io/badge/python-3.9+-blue.svg" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg" alt="Platform">
  <img src="https://img.shields.io/badge/version-1.5.1-orange.svg" alt="Version 1.5.1">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT License">
</p>

<h1 align="center">抖音直播监听器</h1>

<p align="center">
  同时监控多个抖音主播，在开播、持续直播和下播时通过 Server 酱³ 推送到手机。
</p>

## 下载与快速开始

Windows 用户可以直接从 [Releases](https://github.com/washls/douyin-live-monitor/releases) 下载 `douyin-monitor-v1.5.1.exe`，不需要安装 Python。

1. 双击 EXE 打开图形界面。程序进入 GUI 后不会保留黑色命令行窗口。
2. 打开“监控设置”，填写 Server 酱³ 推送 URL 并保存。
3. 点击“新增主播”，填写主播主页链接和便于识别的名称。
4. 点击“开始监控”。任务状态表会持续显示每个主播的检测结果。

Server 酱³ 推送 URL 可以在 [sc3.ft07.com](https://sc3.ft07.com) 登录后获取。配置只保存在程序所在目录，不会由本程序上传到其他位置。

## v1.5.1

这一版集中修复安全、误报和长期运行资源占用问题。日志现在统一脱敏，不再输出推送密钥或 Cookie 内容；旧 HTTP 抖音链接会自动升级为 HTTPS，短链接重定向仅允许可信公网目标。无法验证的直播分享链接会保持“未知”，不会触发开播通知。

普通离线轮询只需主接口和 Webcast 双源确认，每 10 轮或最长 5 分钟执行一次完整巡检。未知结果使用渐进退避，检测策略按 45 秒启动预算依次执行，减少接口请求、线程和连接占用。CLI 与 GUI 现在共用同一配置校验、应用装配和调度实现。

如需回退，可以继续使用 [v1.5.0](https://github.com/washls/douyin-live-monitor/releases/tag/v1.5.0)。v1.5.1 保持原配置字段兼容，没有不可逆的数据迁移。

## 主要功能

- 同时监控多个主播，每个主播使用独立检测状态，一个任务异常不会阻塞其他任务。
- 自动识别开播、持续直播、切换直播间和下播，并按设置发送通知。
- 默认最多同时检测 2 个主播，检测任务错峰执行，适合长时间运行。
- 主接口暂时返回空响应时会刷新该主播会话并重试，不会直接误判为下播。
- 双源确认离线后降低普通轮询请求量，同时保留周期性完整巡检。
- 未知状态采用渐进退避；明确结果会立即恢复正常检测间隔。
- 推送请求遇到超时或连接中断时不会自动重复提交，减少服务端已接收后再次推送的风险。
- 详细日志自动轮转，单个主播的本次运行日志可以在 GUI 中单独查看。
- 支持 Windows 图形界面，也保留完整命令行模式。

## 图形界面使用说明

### 管理主播

在左侧选择主播后，可以在“主播详情”中修改名称、主页链接和启用状态。停用的主播会保留配置，但下次开始监控时不会创建任务。

同一抖音账号如果通过不同链接重复添加，程序会在启动时根据解析出的账号标识识别重复项，只保留列表中靠前的任务继续运行。

### 查看任务状态和实时日志

“运行状态”表显示主播当前状态、最后检测时间和所用检测方法。表头与内容均居中显示。

双击任意主播行，可以打开该主播本次监控的实时运行日志。日志窗口不会混入其他主播的检测记录。停止监控后仍可查看本次日志，再次开始监控时会清空旧会话日志。

### 停止一个主播或全部主播

开始监控后，在“运行状态”中选中主播，再点击“停止所选主播”，只会结束该主播的本次监控，其他任务继续运行。该主播会保持“已停止”状态，在途检测结果也不会覆盖它。

顶部“停止全部”用于结束当前所有任务。重新点击“开始监控”时，所有仍处于启用状态的主播都会重新加入监控。

## 通知规则

| 场景 | 默认行为 |
|------|----------|
| 首次检测到开播 | 立即发送一次开播通知 |
| 持续直播 | 每 10 分钟提醒一次，单次直播最多 3 次 |
| 切换直播间 | 房间 ID 变化后按新一轮直播发送开播通知 |
| 检测到下播 | 发送下播通知，可以在设置中关闭 |
| 每日亲密度提醒 | 23:57、23:58、23:59 各发送一条，正文汇总当时所有已确认直播中的主播 |
| 推送结果不明确 | 不自动重放相同请求，避免重复通知 |
| 单个主播连续异常 | 连续 10 次明确检测异常后暂停该主播，其他任务继续运行 |

每日亲密度提醒在多主播模式下按分钟合并。即使多个主播同时直播，同一分钟也只会收到一条提醒，不会按主播数量重复发送。

## 配置说明

程序首次运行时会创建 `config.json`。推荐在 GUI 中修改设置，也可以关闭程序后手动编辑。

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
| `push_url` | Server 酱³ 完整推送 URL | 空 |
| `check_interval` | 每个主播的检测间隔，单位为秒 | `30` |
| `notify_on_stream_end` | 是否发送下播通知 | `true` |
| `repeat_notify_interval` | 持续直播提醒间隔，单位为秒 | `600` |
| `max_repeat_notifications` | 单次直播最多发送几次持续提醒 | `3` |
| `startup_notify` | 启动监控时是否发送汇总通知 | `false` |
| `enable_daily_intimacy_reminder` | 是否启用每日亲密度提醒 | `true` |
| `max_concurrent_checks` | 同时执行的检测数量，范围为 1 到 8 | `2` |
| `streamers` | 主播配置列表，最多保存 100 项 | 空列表 |

建议将检测间隔保持在 30 秒或更长。过于频繁的访问更容易触发平台风控，也会增加本机和网络负担。

## 从源码运行

需要 Python 3.9 或更高版本。

```bash
git clone https://github.com/washls/douyin-live-monitor.git
cd douyin-live-monitor
python -m pip install -r requirements.txt
python gui_entry.py
```

Windows 也可以运行 `setup.bat` 安装依赖。

### 常用命令

```bash
# 打开图形界面
python gui_entry.py

# 持续监控全部已启用主播
python monitor.py

# 检测一次后退出
python monitor.py --once

# 测试 Server 酱³ 推送
python monitor.py --test

# 查看主播列表
python monitor.py --list-streamers

# 添加主播
python monitor.py --add-streamer "https://www.douyin.com/user/xxxxx" --label "主播名称"

# 停用、启用或删除主播
python monitor.py --disable-streamer STREAMER_ID
python monitor.py --enable-streamer STREAMER_ID
python monitor.py --remove-streamer STREAMER_ID --yes

# 查看详细日志或保存原始响应
python monitor.py --verbose
python monitor.py --debug

# 查看版本
python monitor.py --version
```

命令行持续监控时，输入 `q` 并回车或按 `Ctrl+C` 可以停止全部任务。指定主播停止功能目前在 GUI 中提供。

给 EXE 传入上述命令行参数时会进入命令行模式。例如：

```powershell
.\douyin-monitor-v1.5.1.exe --once
.\douyin-monitor-v1.5.1.exe --list-streamers
```

## 日志与排障

程序目录中的 `monitor.log` 保存详细运行记录。单个日志文件最大 2 MB，最多保留当前文件和 2 个备份，总占用约 6 MB。

遇到状态异常时，可以按以下顺序检查：

1. 在 GUI 中双击对应主播，确认最近一次检测使用的方法和错误信息。
2. 运行 `python monitor.py --once --verbose`，观察一次完整检测过程。
3. 只有在需要分析接口原始响应时才使用 `--debug`。
4. 提交问题时可以附上相关时间段的日志，但请先移除推送 URL、Cookie 和其他个人信息。

`--debug` 会把接口响应保存到 `debug_dumps/`。这些文件可能含主播标识和接口参数，排查结束后请妥善保管或删除。

### 常见问题

#### 双击 EXE 后仍看到命令行窗口

请确认下载的是当前 Release 中的正式 EXE。默认双击会进入 GUI 并隐藏程序自建的控制台；从已有终端带参数启动时，命令行窗口会保留，这是预期行为。

#### 推送测试失败

重新从 [Server 酱³](https://sc3.ft07.com) 复制完整推送 URL，确认网络可访问该服务。推送 URL 的格式应类似 `https://用户标识.push.ft07.com/send/发送密钥.send`。

#### 主播明明开播却显示未开播

抖音接口和页面结构可能变化。程序会依次尝试主接口、Webcast 接口、用户主页和分享页等多种方法。先查看该主播实时日志；如果所有方法都无法确认，程序会保留“异常”而不是直接当作下播，并在后续轮次继续检测。

#### 如何回退版本

旧版 EXE 会继续保留在 [Releases](https://github.com/washls/douyin-live-monitor/releases)。当前配置结构向后兼容，没有不可逆的数据迁移。回退前请先关闭正在运行的程序，并备份本地 `config.json`。

## 数据与隐私

- `config.json` 可能包含完整推送 URL，不要公开上传。
- `monitor.log`、`debug_dumps/` 和 `.monitor_state.json` 可能包含主播标识或本机运行信息。
- 项目的 Git 忽略规则会排除本地配置、日志、调试响应和构建产物。
- 程序不会把主播配置上传到项目仓库，网络请求仅用于查询直播状态和发送已配置的通知。

## 构建与测试

```bash
python -m pytest -q
python -m ruff check .
python -m PyInstaller --clean --noconfirm douyin-monitor.spec
```

开发与 CI 依赖固定在 `requirements-dev.txt`。Windows CI 会在 Python 3.9 和 3.12 上运行测试、覆盖率报告、Ruff 和编译检查，并在 Python 3.12 上验证 PyInstaller 构建；覆盖率当前仅报告，不设置全局阈值。

正式 Windows EXE 使用 `douyin-monitor.spec` 构建。发布前应同时验证 GUI 双击启动、命令行参数、版本输出和关键监控流程。

## 检测方式

程序先使用抖音用户接口和 Webcast 接口双源确认。直播会立即返回；双源明确离线时结束普通轮询；未知、冲突或周期巡检到期时，才按顺序尝试用户主页、IES 分享页和直播间页面。只有得到明确结果时才更新为“直播中”或“未开播”。

检测依赖平台公开页面和接口行为，平台变更可能导致暂时不可用。遇到问题时请提供脱敏日志和可复现时间段，便于定位具体检测链路。

## 参与开发

欢迎提交 Issue 或 Pull Request。修改代码后，请至少运行测试和 Ruff 检查，并避免提交以下内容：

- 真实 `config.json` 和推送密钥
- 日志、调试响应和崩溃转储
- `build/`、`dist/` 等构建产物
- 含本机绝对路径的临时文件

## 许可与说明

本项目采用 [MIT License](LICENSE)。使用本工具时，请遵守抖音及相关服务的条款，并自行评估自动化访问和通知服务带来的风险。

项目使用或参考了以下开源项目：

| 项目 | 用途 |
|------|------|
| [DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder) | 签名算法分析参考 |
| [requests](https://github.com/psf/requests) | HTTP 客户端 |
| [PyInstaller](https://github.com/pyinstaller/pyinstaller) | Windows 可执行文件构建 |
| [Server 酱³](https://sc3.ft07.com) | 手机消息推送服务 |

历史版本和正式构建产物请查看 [Releases](https://github.com/washls/douyin-live-monitor/releases)。
