# 哈工大（深圳）羽毛球场预约工具

使用 Python、Playwright 和 Microsoft Edge 运行的羽毛球场预约脚本。默认执行安全 Dry Run；真实预约必须同时启用配置、环境确认和 `--live` 参数。

请仅使用自己的账号，并遵守学校信息系统和场馆的相关规定。不要把账号、密码、浏览器资料或运行日志提交到 Git。

## 当前策略

- 时间优先级：`15:00–17:00` → `19:00–21:00` → `20:00–22:00` → `10:00–12:00`。
- 场地优先级：`2 > 3 > 1 > 4 > 5 > 6`。
- 优先预约同一场地连续两小时，其次允许不同场地拼成连续两小时，最后才退化为单小时。
- 第一小时成功、第二小时被抢时，立即按场地优先级尝试其他场地的相邻小时；全部不可用后才选择其他时间段。
- 每次运行最多成功两笔；服务器“尚未开放”的临时拒绝不占用预约额度。
- 第一笔成功后直接返回预约页，不重新绕行信息门户。

## 为什么标准延时是 70 秒

网站标称每天 `08:00` 开放次日预约，但连续三天的提交日志显示，后端实际放行时间稳定在本机时间 `08:01:17` 左右：

| 日期 | 最后一次拒绝 | 第一次成功 |
|---|---:|---:|
| 2026-10-05 | 08:01:15.299 | 08:01:17.124 |
| 2026-10-06 | 08:01:15.392 | 08:01:17.173 |
| 2026-10-07 | 08:01:16.372 | 08:01:18.435 |

因此标准命令使用 `--release-delay-seconds 70`：程序仍在 `07:59` 打开并登录门户，但到 `08:01:10` 才进入预约板块。页面处理和首次扫描约需两秒，随后程序会继续短间隔重试，兼顾速度并显著减少开放前的无效请求。

`70` 是当前实测推荐值，不是网站官方参数。如果后端开放时间发生变化，可以修改该数字；省略参数时会按标称的 `08:00:00` 进入预约板块。

## 环境要求

- Python 3.11 或更高版本。
- Microsoft Edge。
- 可正常访问哈工大（深圳）信息门户的网络环境和本人账号。
- 图形桌面环境。当前脚本使用可见的 Edge，不适合没有桌面的 Linux 服务器。

## 获取代码

下面的仓库地址是占位符，请替换成实际 Git 地址。命令会统一把代码克隆到 `badminton_booking_live` 目录。

```bash
git clone https://github.com/Zeroran05/badminton_booking_live.git 
cd badminton_booking_live
```

如果代码已经下载，直接进入包含 `main.py` 和 `requirements.txt` 的项目根目录即可。

## macOS 部署

在“终端”中进入项目目录，然后运行：

```bash
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install msedge
cp .env.example .env
```

如果 Microsoft Edge 已安装，Playwright 的安装步骤会复用或确认对应浏览器通道。

### macOS 标准运行命令

```bash
cd /path/to/badminton_booking_live
caffeinate -is env LIVE_BOOKING_CONFIRM=I_UNDERSTAND_THIS_CREATES_REAL_BOOKINGS .venv/bin/python main.py --live --release-delay-seconds 70
```

`caffeinate -is` 会阻止空闲睡眠，并在接通电源时阻止系统睡眠。MacBook 应接通电源、保持开盖和用户登录，建议使用command+control+Q手动锁屏，但不要合盖和关机。

## Windows / Ubuntu 部署

### Windows 10/11（PowerShell）

```powershell
git clone https://github.com/YOUR_NAME/YOUR_REPOSITORY.git badminton_booking_live
Set-Location .\badminton_booking_live
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install msedge
Copy-Item .env.example .env
```

标准运行命令：

```powershell
Set-Location "C:\path\to\badminton_booking_live"
$env:LIVE_BOOKING_CONFIRM = "I_UNDERSTAND_THIS_CREATES_REAL_BOOKINGS"
.\.venv\Scripts\python.exe .\main.py --live --release-delay-seconds 70
```

运行前请在 Windows“设置 → 系统 → 电源和电池”中确保接通电源时不会自动睡眠。锁屏通常不影响已经运行的程序，但注销、关机或系统睡眠会中断浏览器自动化。

### Ubuntu 桌面版

如果系统尚未安装 Python 虚拟环境组件，先运行：

```bash
sudo apt update
sudo apt install -y python3 python3-venv
```

然后在项目目录运行：

```bash
git clone https://github.com/YOUR_NAME/YOUR_REPOSITORY.git badminton_booking_live
cd badminton_booking_live
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install --with-deps msedge
cp .env.example .env
```

标准运行命令：

```bash
cd /path/to/badminton_booking_live
systemd-inhibit --what=sleep --why="Badminton booking" env LIVE_BOOKING_CONFIRM=I_UNDERSTAND_THIS_CREATES_REAL_BOOKINGS .venv/bin/python main.py --live --release-delay-seconds 70
```

Ubuntu 必须保持图形桌面会话登录，并允许 Edge 窗口启动。若系统没有 `systemd-inhibit`，请在电源设置中手动关闭自动睡眠。

## 配置本机账号

打开刚复制出的 `.env`，填写：

```dotenv
CONTACT_PHONE="手机号"
PORTAL_USERNAME="学号或工号"
PORTAL_PASSWORD="密码"
LIVE_BOOKING_CONFIRM=I_UNDERSTAND_THIS_CREATES_REAL_BOOKINGS
```

`.env` 只应保存在本人电脑上。在 macOS Finder 中可以按 `Command + Shift + .` 显示隐藏文件。

如需调整场地、时间或最大预约数，编辑 `config.yaml`。真实运行前确认：

```yaml
booking:
  live_enabled: true
```

## 先执行安全测试

Dry Run 会扫描并选择方案，但阻止真实提交。

macOS / Ubuntu：

```bash
.venv/bin/python main.py --run-now --once
```

Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe .\main.py --run-now --once
```

运行自动化测试：

```bash
.venv/bin/python -m pytest -q
```

Windows PowerShell 对应命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## 本项目作者电脑上的命令

下面的绝对路径仅适用于当前开发电脑，其他使用者不应照抄，应使用前文的通用项目路径。

```bash
cd /Users/zhuran/Desktop/badminton_booking_live
caffeinate -is env LIVE_BOOKING_CONFIRM=I_UNDERSTAND_THIS_CREATES_REAL_BOOKINGS .venv/bin/python main.py --live --release-delay-seconds 70
```

立即真实执行仅用于明确需要现在预约时。`--run-now` 会忽略定时等待和 `70` 秒延时：

```bash
cd /Users/zhuran/Desktop/badminton_booking_live
env LIVE_BOOKING_CONFIRM=I_UNDERSTAND_THIS_CREATES_REAL_BOOKINGS .venv/bin/python main.py --live --run-now
```

## macOS 定时启动（可选）

仓库内的 `launchd/com.zhuran.badminton-booking.plist` 是作者电脑的示例，包含作者本机绝对路径和标准 `70` 秒延时。其他用户使用前必须把其中的 Python 路径、`main.py` 路径、工作目录和日志路径替换为自己的绝对路径。

安装、检查和卸载：

```bash
mkdir -p "$HOME/Library/LaunchAgents"
cp launchd/com.zhuran.badminton-booking.plist "$HOME/Library/LaunchAgents/"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.zhuran.badminton-booking.plist"
launchctl print "gui/$(id -u)/com.zhuran.badminton-booking"
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.zhuran.badminton-booking.plist"
```

每天 `07:57` 自动唤醒：

```bash
sudo pmset repeat wakeorpoweron MTWRFSU 07:57:00
pmset -g sched
```

取消重复唤醒：

```bash
sudo pmset repeat cancel
```

自动唤醒不会输入登录密码，也不能可靠覆盖普通合盖休眠。最可靠状态仍是接电、开盖、已登录并锁屏。

## 交给 Codex 部署

可以在仓库根目录把下面这段话直接发给 Codex：

> 请检测当前操作系统，并按照 README 的 macOS、Windows 或 Ubuntu 部署步骤安装此项目。创建项目内的 `.venv`，安装 `requirements.txt` 和 Microsoft Edge Playwright 通道，复制 `.env.example` 为 `.env`，但不要读取、填写或输出任何账号密码，也不要执行 `--live`。最后运行测试并告诉我如何在本机填写 `.env`。正式命令必须保留 `--release-delay-seconds 70`。

## 结果与排错

- `BOOKING_SUCCESS`：服务器明确返回预约成功。
- `RELEASE_NOT_OPEN`：服务器尚未真正开放，程序会继续短间隔重试。
- `logs/latest.log`：完整运行日志和毫秒级时间。
- `logs/latest_scan.json`：最近一次场地状态和选择方案。
- `screenshots/submit_result_*.png`：提交结果页面。

`.env`、`.venv`、浏览器资料、日志和截图均已写入 `.gitignore`，不会在正常的 `git add .` 中被提交。
