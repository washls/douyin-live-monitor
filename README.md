<div align="center">
  <a href="https://github.com/washls/douyin-live-monitor">
    <img width="420" src="assets/app_icon.png" alt="多平台直播监控与开播提醒工具插画">
  </a>

  <h1>多平台直播监控与开播提醒工具</h1>

<p>
  <strong>多平台直播监听器</strong> · Python 3.9+ · Windows / Linux / macOS · v1.8.0 · PolyForm NC 1.0.0
</p>

<p>
  在一个程序中监控抖音、哔哩哔哩、虎牙和斗鱼的公开主播，实验支持快手。无需登录直播平台，添加主播链接即可查看开播状态。<br>
  通过 Server 酱³ 接收开播、持续直播、下播和故障恢复提醒，通知附带对应平台的观看链接。<br>
  支持 Windows 免安装图形界面、托盘驻留、开机自启和命令行模式，适合多主播常驻监控。
</p>

<p>
  免费使用，源代码公开，采用 PolyForm Noncommercial License 1.0.0，仅授权非商业用途。<br>
  项目地址：<a href="https://github.com/washls/douyin-live-monitor">github.com/washls/douyin-live-monitor</a>
</p>

<p>
  <strong><a href="https://github.com/washls/douyin-live-monitor/releases/latest">下载 Windows 免安装版</a></strong>
</p>
</div>

## 下载与快速开始

Windows 用户可以直接从 [最新版下载页](https://github.com/washls/douyin-live-monitor/releases/latest) 下载 `douyin-monitor-v1.8.0.exe`，不需要安装 Python。

1. 双击 EXE 打开图形界面。任务栏只保留图形界面，不会创建终端窗口。
2. 打开“监控设置”，填写 Server 酱³ 推送 URL 并保存。
3. 点击“新增主播”，填写支持的平台主播链接和便于识别的名称，程序会自动识别平台。
4. 点击“开始监控”。任务状态表会持续显示每个主播的检测结果。
5. 如需后台常驻，可在“监控设置”中把关闭窗口行为改为“隐藏到任务栏托盘”。

Server 酱³ 推送 URL 可以在 [sc3.ft07.com](https://sc3.ft07.com) 登录后获取。配置只保存在程序所在目录，不会由本程序上传到其他位置。

## v1.8.0

新增哔哩哔哩、虎牙和斗鱼直播状态监控，实验支持快手。各平台通过公开页面或接口查询，无需用户登录。主播列表和运行状态显示平台，开播、持续直播、下播以及故障恢复通知均标明平台，并使用对应平台的观看链接。

| 平台 | 支持的链接 | 支持情况 |
|------|------------|----------|
| 抖音 | 主播主页及现有分享链接 | 正式支持 |
| 哔哩哔哩 | `https://live.bilibili.com/房间号` | 正式支持，支持短房间号 |
| 虎牙 | `https://www.huya.com/房间号或别名` | 正式支持 |
| 斗鱼 | `https://www.douyu.com/房间号或别名` | 正式支持，轮播不触发开播提醒 |
| 快手 | `https://live.kuaishou.com/u/主播标识` | 实验支持，匿名访问可能受限 |
| 视频号 | 微信视频号 | 当前不支持免登录监控 |

访问受限、网络错误或页面结构变化时显示未知或异常，不会当作下播。平台限流只暂停对应平台的请求，其他平台继续检测。默认全局并发为 2，同平台同时最多执行一项检测；B站、虎牙、斗鱼至少间隔 30 秒，快手至少间隔 60 秒。主播较多时实际检测间隔可能增加，不保证固定秒数内收到开播通知。

直播房间与直播场次分别处理。平台提供可靠场次标识时可识别同房间再次开播；缺少标识时，须先确认下播再识别新开播。两次检测之间发生的短暂开下播可能无法发现。每日亲密度提醒仅汇总抖音主播。

旧抖音配置可以直接使用。首次保存升级配置时，程序在原配置旁创建 `config.json.pre-v1.8.0.bak`（自定义配置使用相同后缀），其中可能包含推送地址，请与原配置一同妥善保管。回退前退出程序，将升级前备份恢复为原配置，再运行 [v1.7.0](https://github.com/washls/douyin-live-monitor/releases/tag/v1.7.0)。旧版无法读取包含其他平台链接的新配置。使用登录自启的用户需在新 EXE 中重新保存自启设置。

自 v1.6.2 起，项目采用 PolyForm Noncommercial License 1.0.0，仅允许非商业用途。v1.6.1 及更早版本曾按 MIT License 发布，已经取得的 MIT 授权不受本次许可证变更影响。

## 主要功能

- 同时监控多个平台的主播，自动识别链接所属平台；每个主播独立检测，一个任务异常不会阻塞其他任务。
- 识别开播、持续直播和下播，通知标明平台并提供观看链接。
- 默认最多同时检测 2 个主播，检测任务错峰执行，适合长时间运行。
- 访问受限、空响应或解析失败时保持未知，不会直接误判为下播；限流只冷却对应平台。
- 抖音保留双源检测、会话刷新和周期性完整巡检，其他平台按明确状态或页面证据判断。
- 未知状态采用渐进退避；明确结果会立即恢复正常检测间隔。
- 推送请求遇到超时或连接中断时不会自动重复提交，减少服务端已接收后再次推送的风险。
- 详细日志自动轮转，单个主播的本次运行日志可以在 GUI 中单独查看。
- 支持 Windows 任务栏托盘、登录自启和单实例保护。
- 支持 Windows 图形界面，也保留完整命令行模式。

## 图形界面使用说明

### 管理主播

在左侧选择主播后，可以在“主播详情”中修改名称、主播链接和启用状态。停用的主播会保留配置，但下次开始监控时不会创建任务。

同一平台的主播如果通过不同链接重复添加，程序会根据解析出的平台和目标标识识别重复项。不同平台的相同数字标识视为不同目标。持续监控保留已成功解析并加入监控的任务，后续解析出的重复项会被跳过；单次检测按配置顺序保留第一项。

### 查看任务状态和实时日志

“运行状态”表显示主播当前状态、最后检测时间和所用检测方法。表头与内容均居中显示。

双击任意主播行，可以打开该主播本次监控的实时运行日志。日志窗口不会混入其他主播的检测记录。停止监控后仍可查看本次日志，再次开始监控时会清空旧会话日志。

### 停止一个主播或全部主播

开始监控后，在“运行状态”中选中主播，再点击“停止所选主播”，只会结束该主播的本次监控，其他任务继续运行。该主播会保持“已停止”状态，在途检测结果也不会覆盖它。

顶部“停止全部”用于结束当前所有任务。重新点击“开始监控”时，所有仍处于启用状态的主播都会重新加入监控。

### 使用任务栏托盘和开机自启

Windows 版启动后会显示任务栏托盘图标。托盘菜单固定提供“打开主窗口”“开始监控”“停止监控”“当前在线主播数量”和“退出程序”。主窗口隐藏后，监控任务和实时状态仍会继续更新。

在“监控设置”的“窗口与系统”区域，可以选择关闭窗口时“退出主程序”或“隐藏到任务栏托盘”。旧配置默认使用“退出主程序”；隐藏主窗口后，只有通过托盘菜单选择“退出程序”并确认，程序才会停止全部监控线程并结束进程。

勾选“Windows 开机自启”后，程序会在当前用户登录 Windows 时静默进入托盘，并自动监控所有已启用主播。该设置跟随当前 EXE 路径；移动文件或升级到新文件名后，需要在新版本中重新勾选。

同一 Windows 会话只允许一个监控实例。再次双击 EXE 会打开已有窗口；如果命令行监控正在运行，GUI 会提示先停止现有实例。`--help`、`--version`、推送测试和主播配置命令不受影响。

## 通知规则

| 场景 | 默认行为 |
|------|----------|
| 首次检测到开播 | 立即发送一次开播通知 |
| 持续直播 | 每 10 分钟提醒一次，单次直播最多 3 次 |
| 切换直播间 | 按平台可靠场次标识识别新直播；抖音保留房间变化规则 |
| 检测到下播 | 发送下播通知，可以在设置中关闭 |
| 每日亲密度提醒 | 23:57、23:58、23:59 各发送一条，正文汇总当时已确认直播中的抖音主播 |
| 推送结果不明确 | 不自动重放相同请求，避免重复通知 |
| 启动网络解析失败 | 等待 10、30、60 秒后重试，此后每 5 分钟重试 |
| 启动或检测持续异常 | 满 5 分钟通知一次，继续检测；原因变化不重新计时 |
| 监控恢复 | 已触发异常通知后，首次确认直播状态时通知一次 |
| 链接无效或安全校验失败 | 暂停该主播并通知一次 |
| 单个主播连续异常 | 连续 10 次明确检测异常后暂停该主播并通知一次，其他任务继续运行 |

异常与恢复通知默认启用，复用现有推送地址。同一事件只发送一次，即使失败或结果未知也不自动重发；断网时手机通知可能无法送达，请查看任务表和日志。故障记录仅保留在本次运行内，重启后重新计时。备用检测接口失败但整体检测成功、主动停止和重复主播跳过均不发送故障通知。

`--once` 只执行一轮，任一启用主播未取得结果时返回非零退出码，不进入持续重试。

每日亲密度提醒在多主播模式下按分钟合并，只包含抖音主播。即使多个主播同时直播，同一分钟也只会收到一条提醒，不会按主播数量重复发送。

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
  "close_action": "exit",
  "streamers": [
    {
      "id": "a1b2c3d4e5f6",
      "url": "https://www.douyin.com/user/xxxxx",
      "platform": "douyin",
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
| `close_action` | 关闭窗口时退出程序或隐藏到托盘 | `exit` |
| `streamers` | 主播配置列表，最多保存 100 项；新增记录含自动识别的 `platform` | 空列表 |

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
python monitor.py --add-streamer "https://live.bilibili.com/123" --label "B站主播"

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
Start-Process -FilePath .\douyin-monitor-v1.8.0.exe -ArgumentList '--once' -NoNewWindow -Wait
Start-Process -FilePath .\douyin-monitor-v1.8.0.exe -ArgumentList '--list-streamers' -NoNewWindow -Wait
```

正式 EXE 使用 Windows GUI 子系统，PowerShell 直接执行时不会自动等待。使用上面的 `Start-Process` 写法可以让命令行输出留在当前终端，并等待任务结束后再返回提示符。

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

请确认下载的是 v1.6.0 或更高版本的正式 EXE。默认双击只会创建 GUI 窗口和任务栏托盘图标；从已有终端带参数启动时，程序会连接该终端以显示命令行输出，这是预期行为。

#### 双击 EXE 后没有出现新窗口

程序可能已经隐藏在任务栏托盘中。再次双击 EXE 会唤醒已有窗口，不会启动第二套监控任务。也可以在 Windows 托盘区域找到蓝底红点图标，点击“打开主窗口”。

#### 推送测试失败

重新从 [Server 酱³](https://sc3.ft07.com) 复制完整推送 URL，确认网络可访问该服务。推送 URL 的格式应类似 `https://用户标识.push.ft07.com/send/发送密钥.send`。

#### 主播明明开播却显示未开播

抖音接口和页面结构可能变化。程序会依次尝试主接口、Webcast 接口、用户主页和分享页等多种方法。先查看该主播实时日志；如果所有方法都无法确认，程序会保留“异常”而不是直接当作下播，并在后续轮次继续检测。

#### 如何回退版本

旧版 EXE 会继续保留在 [Releases](https://github.com/washls/douyin-live-monitor/releases)。回退前请关闭正在运行的程序。v1.8.0 新增平台配置需恢复升级前的本地备份，随后启动旧版。

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

抖音检测先使用用户接口和 Webcast 接口双源确认。直播会立即返回；双源明确离线时结束普通轮询；未知、冲突或周期巡检到期时，才按顺序尝试用户主页、IES 分享页和直播间页面。只有得到明确结果时才更新为“直播中”或“未开播”。

B站、虎牙和斗鱼读取明确直播状态，并将轮播识别为未开播；快手仅在页面具有明确证据时确认直播或离线。平台访问均为匿名查询，不读取浏览器登录信息。

检测依赖平台公开页面和接口行为，平台变更可能导致暂时不可用。遇到问题时请提供脱敏日志和可复现时间段，便于定位具体检测链路。

## 参与开发

欢迎提交 Issue 或 Pull Request。修改代码后，请至少运行测试和 Ruff 检查，并避免提交以下内容：

- 真实 `config.json` 和推送密钥
- 日志、调试响应和崩溃转储
- `build/`、`dist/` 等构建产物
- 含本机绝对路径的临时文件

## 许可与说明

自 v1.6.2 起，本项目采用 [PolyForm Noncommercial License 1.0.0](LICENSE)，源代码公开，但仅授权非商业用途。复制、修改或分发时必须同时保留许可证条款，以及 [NOTICE](NOTICE) 中以 `Required Notice:` 开头的项目声明和官方仓库地址。

此前已经发布的版本继续适用其发布时附带的许可证，新的许可证不会追溯撤销已经授予的旧版许可。使用本工具时，请遵守各直播平台及通知服务的条款，并自行评估自动化访问和通知服务带来的风险。

项目使用或参考了以下开源项目：

| 项目 | 用途 |
|------|------|
| [DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder) | 签名算法分析参考 |
| [requests](https://github.com/psf/requests) | HTTP 客户端 |
| [PyInstaller](https://github.com/pyinstaller/pyinstaller) | Windows 可执行文件构建 |
| [pystray](https://github.com/moses-palmer/pystray) | Windows 任务栏托盘 |
| [Pillow](https://github.com/python-pillow/Pillow) | 托盘图标绘制 |
| [Server 酱³](https://sc3.ft07.com) | 手机消息推送服务 |

第三方依赖的许可证说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

历史版本和正式构建产物请查看 [Releases](https://github.com/washls/douyin-live-monitor/releases)。
