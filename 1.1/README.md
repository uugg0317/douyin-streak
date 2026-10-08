# 抖音续火花 · 单账号本机版

这是基于 FastAPI 和 Playwright 的单账号续火花工具。启动后打开管理页面即可进入，查看配置、好友台账和运行日志。管理页面与 API 无需管理密码、访问令牌或会话 Cookie。服务默认只监听 `127.0.0.1`，也支持 `localhost` 和 `::1`。

抖音账号仍需由你扫码登录或上传自己的 Playwright `state.json`；公开源码中不包含抖音登录态、好友数据、邮箱授权码或真实 `.env`。

## 本机运行

使用 Python 3.10+。在本目录打开终端，创建独立虚拟环境并安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe app.py
```

然后打开 [本机管理页面](http://127.0.0.1:8000)。无需填写管理登录配置。可按需将 `.env.example` 复制为 `.env`，设置 `PORT`、时区、浏览器预热等选项。`HOST` 只支持回环地址。

Linux 可执行 `bash start.sh`，首次启动会创建本机配置、安装 Python 依赖和 Chromium。脚本或桌面构建安装依赖时需要网络连接。

## 使用管理页面

1. 打开“凭证”页，选择“扫码提取登录态”，用你自己的抖音 App 扫码并确认登录；也可以上传自己提取的 `state.json`。
2. 在“好友”页同步联系人，勾选好友并设置消息。
3. 在“定时”页设置每天发送时间、浮动时间、间隔和自动发送开关。
4. 在“概览”页查看状态，按需执行“立即续火花”或干跑测试；“日志”页显示执行记录。
5. 如需邮件提醒，在“邮箱”页填写自己的 SMTP 参数和授权码。

抖音凭证会过期；过期后回到“凭证”页重新扫码或上传。执行真实发送前请确认好友选择和发送内容。

## 目录说明

- `app.py`、`routers/`：应用入口和 HTTP 接口。
- `core/`：浏览器、发送、定时调度、配置与台账逻辑。
- `static/`：本地管理页面及前端依赖。
- `desktop/`：Windows 桌面版构建与启动器，见 [桌面说明](desktop/README.md)。
- `data/`：运行后产生的登录态、配置、台账与日志，公开仓库只包含空目录占位文件。

`data/state.json` 是自己的抖音账号登录凭证，`ledger.json` 和 `config.json` 可能含好友资料。备份时请单独妥善保存。真实 `.env`、登录态、二维码、依赖环境和运行数据均被 `.gitignore` 排除。

## Linux 后台运行

`deploy.sh` 和 `douyin-streak.service` 可安装本机 systemd 服务，使用独立服务用户，保留已有 `data/`、环境文件和浏览器缓存。部署完成后仍监听回环地址。如果在远程 Linux 主机运行，使用 SSH 本地端口转发访问：

```bash
ssh -L 8000:127.0.0.1:8000 user@your-server
```

在自己电脑打开 `http://127.0.0.1:8000`。无需将管理页面通过 Nginx 暴露到公网。

## 本地验证

```powershell
python -B -m unittest discover -s tests -v
```

回归测试使用独立临时数据目录。直接访问测试会启动随机本机端口的临时服务，并替换调度器及看门狗，不执行真实抖音或 SMTP 操作。联调脚本 `_test_refactor_api.py` 会修改你正在运行的服务配置，需要显式设置 `RUN_LIVE_API_TESTS=1`，请仅对测试实例使用。
