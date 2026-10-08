# 抖音续火花 · 本地免登录公开版

使用 Python、FastAPI 和 Playwright 的本地抖音续火花工具。管理后台启动后直接进入，无需管理密码、令牌或 URL 登录。默认地址为 **http://127.0.0.1:8000**，仅接受本机访问。

适合使用者在自己的电脑上管理已获授权的抖音账号：获取自己的登录态、同步和选择好友、查看运行状态、调整任务参数、先做干跑演练，再自行决定是否运行发送和定时任务。

## 选择一个版本

| 目录 | 适用场景 | 使用说明 |
|---|---|---|
| `1.1/` | 管理一个抖音账号 | [单账号本地版](1.1/README.md) |
| `多账号版/` | 账号独立保存、集中查看任务和备份 | [多账号本地版](多账号版/README.md) |
| `cookie_extract/` | 单独获取自己账号的 Playwright 登录态 | [登录态获取流程](cookie_extract/README.md) |

## 快速开始（Windows PowerShell）

在仓库根目录选择一个版本，例如：

```powershell
cd 多账号版
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe app.py
```

打开 **http://127.0.0.1:8000** 即可直接进入后台。Python 需要 3.10+。单账号版把第一条命令改为 `cd 1.1`；具体功能和桌面构建说明见各版本 README。默认不需要 `.env`，需要调整端口时再使用对应 `.env.example`。

管理后台免登录，**抖音账号仍需使用者自己扫码登录或上传自己的登录态**。登录态和运行数据只在使用者本机生成；仓库不附带可用 Cookie、账号、好友名单或邮件凭据。

## 仓库内容与本地数据

本仓库只发布当前源码及示例配置，包含单账号服务、多账号控制台、登录态提取脚本、前端静态资源、桌面构建脚本和隔离测试。原项目历史没有随该源码快照发布。

真实 `.env`、登录态 JSON、二维码截图、账号与好友数据、日志、虚拟环境和构建产物已加入 `.gitignore`。`data/.gitkeep` 只保留空目录。请在自己的或已获授权的账号上使用，并在发送前检查好友选择与参数。
