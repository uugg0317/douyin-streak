# 抖音续火花 · 多账号本地版

基于 FastAPI、Playwright 和 Vue 的多账号管理后台。启动后打开本机地址，直接进入控制台，无需管理密码、令牌或 URL 登录。

## 功能与适用场景

适合在自己的电脑上管理一个或多个已获授权的抖音账号。可添加账号、为各账号获取或上传自己的登录态、采集并选择好友、调整发送间隔和数量、先执行干跑演练，再按自己的选择运行发送任务。还提供任务状态、运行日志、备份和外观设置。

管理后台免登录，抖音账号仍需使用者自己登录。仓库不包含可直接使用的抖音 Cookie、账号列表、好友资料或邮件配置。

## 本地运行（Windows PowerShell）

需要 Python 3.10+。在仓库根目录打开 PowerShell：

```powershell
cd 多账号版
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe app.py
```

随后打开 **http://127.0.0.1:8000**，直接进入多账号控制台。默认不需要 `.env`；需要换端口时，可复制 `.env.example` 为 `.env` 后修改 `PORT`。默认 `HOST=127.0.0.1`，本版本仅接受本机回环请求。

macOS/Linux 可使用 `python3 -m venv .venv`，然后通过 `.venv/bin/python` 执行相同安装与启动命令；也可执行 `bash start.sh`。

## 第一次使用

1. 添加账号，并在「凭据」页选择该账号。
2. 点击获取登录态，在浏览器窗口里用自己的抖音 App 扫码；核对目标账号后保存。也可上传自己取得的 Playwright 登录态 JSON。
3. 同步好友，在「好友」页选择发送对象。
4. 在「参数」和「任务」页检查设置，先运行干跑演练，再自行决定是否启用发送及定时任务。

`data/` 会在本机生成并保存登录态和运行数据，已被 Git 忽略。请勿把生成的数据文件提交到公开仓库。

## 目录

| 路径 | 内容 |
|---|---|
| `app.py` / `bootstrap.py` | 本机 Web 服务与环境设置 |
| `core/` | 账号隔离、任务编排、发送、数据存储、备份和登录态获取 |
| `static/` | 多账号管理界面与本地前端依赖 |
| `desktop/` | Windows 桌面版构建与启动脚本 |
| `tests/` | 使用隔离数据和模拟服务的回归检查 |
| `data/` | 使用者运行时创建的本地数据 |

Windows 打包方式见 [桌面版说明](desktop/README.md)。本公开版默认在本机运行，部署脚本不会安装公网服务。
