# 首次部署指南

> 把 social-auto-upload 装到你自己机器上，并接入 WorkBuddy 用自然语言驱动多平台发布。
> 适用：Windows 10+ / macOS 12+ / Linux；Python 3.10~3.12。

---

## 一、环境要求

| 项 | 要求 |
|---|---|
| 操作系统 | Windows 10+ / macOS 12+ / Linux |
| Python | 3.10 ~ 3.12（推荐 3.12） |
| 磁盘 | 至少 2GB（含 Chromium 浏览器下载） |
| 网络 | 能访问 GitHub 与各平台官网（国内直连即可，无需代理） |

---

## 二、一键装环境

### 1. 安装 Python 3.12（如未装）
- macOS：`brew install python@3.12`
- Windows：https://www.python.org/downloads/release/python-3120/ （勾选 "Add to PATH"）
- 验证：`python3.12 --version`

### 2. 运行一键脚本
在项目根目录打开终端：
```bash
python3.12 setup_env.py
```

脚本会自动完成：
1. 创建虚拟环境 `.venv`
2. 安装 `sau` + `playwright` + `fastmcp` 等依赖
3. 下载 Chromium 浏览器（约 150M）
4. 从 `conf.example.py` 生成 `conf.py`

> **镜像加速已内置**：pip 走清华源、Chromium 走 npmmirror，国内也能快速装完。
> 若某个源仍慢，可分别 `export PIP_INDEX_URL=...` / `export PLAYWRIGHT_DOWNLOAD_HOST=...` 后重跑。

### 3. 验证安装
```bash
# macOS/Linux
.venv/bin/sau --help
# Windows
.venv\Scripts\sau --help
```
能看到平台列表（douyin/xiaohongshu/kuaishou/bilibili/tencent/pinduoduo/sohu/weibo...）即成功。

---

## 三、接入 WorkBuddy MCP

打开 `~/.workbuddy/mcp.json`（Windows 在 `C:\Users\<你>\.workbuddy\mcp.json`），在 `mcpServers` 加一条：

```json
{
  "mcpServers": {
    "social-auto-upload": {
      "command": "C:/path/to/social-auto-upload/.venv/Scripts/python.exe",
      "args": ["C:/path/to/social-auto-upload/mcp_server/server.py"],
      "env": { "MCP_TRANSPORT": "stdio" },
      "disabled": false
    }
  }
}
```

> macOS/Linux：`command` 换成 `.../.venv/bin/python`，`args` 换成 `.../mcp_server/server.py`。

重启 WorkBuddy，连接器里确认 `social-auto-upload` 显示 connected。

---

## 四、部署后第一步：绑定账号

看《账号绑定与多账号管理指南》：
- 7 个平台弹浏览器扫码（`--headed`）
- **B站是终端二维码**（特殊，不加 `--headed`）

绑定任意一个平台后即可对话驱动发布。

---

## 五、常见问题

| 问题 | 解决 |
|---|---|
| `setup_env.py` 下载慢 | 确认走的是国内镜像；或手动 export 源后重跑 |
| Chromium 下载失败 | `PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright` 后重跑 |
| MCP 显示 disconnected | 检查 mcp.json 路径是否正确、venv 是否装好 |
| 发布时报 cookie 失效 | 重新绑定账号（见账号指南） |
| B站上传报证书错误 | 关闭代理/VPN 后重试（B站国内直连） |
