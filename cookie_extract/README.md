# 获取自己的抖音登录态

本目录提供独立的 Playwright 流程：打开 Chromium，让使用者自行扫码登录抖音，再把登录态保存为本地 JSON。管理后台免登录不影响抖音账号本身的登录要求。

## 本地运行（Windows PowerShell）

需要 Python 3.10+。从仓库根目录执行：

```powershell
cd cookie_extract
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe 提取cookie_flow.py
```

浏览器出现后，用自己的抖音 App 扫码并确认。脚本检测登录完成后输出 `state.json`，可在单账号或多账号后台导入给对应账号。

## 可选参数

```powershell
.\.venv\Scripts\python.exe 提取cookie_flow.py --headless
.\.venv\Scripts\python.exe 提取cookie_flow.py --out my_state.json --screenshot qr.png --timeout 120
```

| 参数 | 默认值 | 用途 |
|---|---|---|
| `--out` | `state.json` | 本地登录态文件 |
| `--screenshot` | `extract_qr.png` | 运行截图位置 |
| `--headless` | 关闭 | 无浏览器窗口时通过截图扫码 |
| `--timeout` | 300 秒 | 等待扫码的最长时间 |

生成的 JSON 与截图可能包含账号登录资料，已加入 `.gitignore`，请只在本机保存和使用，不要提交到公开仓库。仓库只提供源码，不附带有效登录态。

缺少浏览器时执行 `python -m playwright install chromium`；扫码等待超时可调整 `--timeout`，或在有窗口模式中手动完成页面要求的验证。

[实现流程](flow.md)说明浏览器登录、检测和保存的数据流；`raw_snippet_app_py_1517_1585.py` 保留早期流程片段用于参考。
