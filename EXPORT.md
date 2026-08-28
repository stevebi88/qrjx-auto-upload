# social-auto-upload + MCP 导出部署指南（同事版）

把本目录（已含我们修复过的抖音定时 bug、多多视频/搜狐号发布器、跨平台 MCP server）放到**你自己机器**上，
按下面步骤即可在 WorkBuddy 里用自然语言驱动多平台、多账号的短视频/图文自动发布与定时。

> 适用：Windows 10+/macOS 12+/Linux；需要 Python 3.12（3.10~3.12 均可）。

> 📦 **拿到的是「离线版」压缩包（social-auto-upload-离线版-*.tar.gz / .zip）？**
> 包内已内置 Python 运行时、依赖与 Chromium 浏览器，**无需再安装任何软件**，
> 请直接按 **`docs/OFFLINE_GUIDE.md`** 操作（解压 → 剥离隔离标记 → 验证 → 配 MCP）。
> 本文件以下内容为「源码方式」联网部署说明，仅在没有离线包时使用。

---

## 一、首次部署（每台机器做一次）

### 1. 安装 Python 3.12
- macOS：`brew install python@3.12`
- Windows：到 https://www.python.org/downloads/release/python-3120/ 安装（勾选 "Add to PATH"）
- 验证：`python3.12 --version`

### 2. 一键装环境
在本目录打开终端，运行：
```bash
python3.12 setup_env.py
```
脚本会自动：建 venv → 装 `sau` + `playwright` + `fastmcp` → 下载 chromium 浏览器 → 生成 `conf.py`。
（约几分钟，主要在下载浏览器；**pip 与 Chromium 均已默认走国内镜像**：pip 走清华源 `https://pypi.tuna.tsinghua.edu.cn/simple`，Chromium 走 npmmirror。若某源仍慢，可分别 `export PIP_INDEX_URL=...` / `export PLAYWRIGHT_DOWNLOAD_HOST=...` 后重跑）

### 3. 接入 WorkBuddy MCP
打开 WorkBuddy 的 `~/.workbuddy/mcp.json`（Windows 在 `C:\Users\<你>\.workbuddy\mcp.json`），
在 `mcpServers` 里加一条（把路径换成你机器上的实际路径）：

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
> macOS / Linux 把 `command` 换成 `.../social-auto-upload/.venv/bin/python`，`args` 换成 `.../mcp_server/server.py`。

重启 WorkBuddy，连接器里确认 `social-auto-upload` 为 connected。

---

## 二、绑定账号（每台机器、每个平台各做一次）

账号 cookie 与**设备绑定**，无法复制别人的 cookie，必须在本机登录。

### 登录方式分两类（⚠️ 重点）

| 平台 | 登录方式 | 命令（在本目录终端跑） |
|---|---|---|
| 抖音 | 弹浏览器扫码 | `sau douyin login --account 账号名 --headed` |
| 小红书 | 弹浏览器扫码 | `sau xiaohongshu login --account 账号名 --headed` |
| 快手 | 弹浏览器扫码 | `sau kuaishou login --account 账号名 --headed` |
| 多多视频 | 弹浏览器扫码 | `sau pinduoduo login --account 账号名 --headed` |
| 搜狐号 | 弹浏览器扫码 | `sau sohu login --account 账号名 --headed` |
| 视频号 | 弹浏览器扫码 | `sau tencent login --account 账号名 --headed` |
| 微博 | 弹浏览器扫码 | `sau weibo login --account 账号名 --headed` |
| **B站** | **终端二维码（不是浏览器！）** | `sau bilibili login --account 账号名` |

**B 站登录特殊说明**（和其它平台完全不同）：
- 不加 `--headed`，登录走 **biliup 工具**（首次自动下载 25M 二进制），生成**终端二维码**
- 必须在你**自己的本地交互式终端**运行（远程 SSH / CI / 无终端环境不行）
- 二维码用 ASCII 字符渲染在终端；**若显示不全/乱码，打开项目目录下的 `qrcode.png`** 用手机扫
- 用 **B 站 App「扫一扫」** 扫码确认
- 扫码成功后 cookie 存到 `cookies/bilibili_账号名.json`

**其余平台**：`--headed` 会弹出浏览器 → 网页上扫码/登录 → 成功后 cookie 自动存到本机 `cookies/` 目录，之后发布全自动（无需再扫码）。

> 也可以在 WorkBuddy 对话里让我执行 `account_bind`（等价于上面的命令）。

---

## 三、日常使用（对话驱动）

在 WorkBuddy 里直接说人话，例如：
- "绑定抖音账号 矩阵号A"
- "把 /路径/demo.mp4 发到抖音『奇人匠心』和『矩阵号A』，标题『夏日穿搭』，话题 穿搭/夏日，明早9点发"
- "上次定时任务都成功了吗？"  → 回查结果

MCP 提供的工具：`account_bind / account_list / account_check / publish_video / publish_note / schedule_task / task_status`。

---

## 三·补 平台发布参数速查（⚠️ 各平台必填参数不同）

| 平台 | 必填参数 | 差异说明 |
|---|---|---|
| 抖音 | `--title --file` | 定时 `--schedule "YYYY-MM-DD HH:MM"` |
| 小红书 | `--title --file` | 图文/视频均可，定时 `--schedule` |
| 多多视频 | `--title --file` | 封面/内容声明/标签自动处理；定时 `--schedule` |
| 搜狐号 | `--title --file` | 定时 `--schedule` |
| 视频号 | `--title --file` | 定时 `--schedule` |
| 微博 | `--title --file` | ⚠️ 暂不支持定时发布（CLI 无 `--schedule`） |
| 百家号 | `--title --file` | ⚠️ 暂不支持定时发布（CLI 无 `--schedule`） |
| **B站** | **`--title --file --desc --tid`** | **`--desc`（简介）和 `--tid`（分区 id）必填**；上传走 biliup API 无需浏览器；定时 `--schedule` |

B 站常用 `--tid` 分区：`160`=知识、`138`=生活、`65`=科技、`155`=音乐、`129`=动画。

> 命令示例：`sau bilibili upload-video --account 奇人匠心 --file videos/demo.mp4 --title "标题" --desc "简介" --tid 160 --tags "标签1,标签2" --schedule "2026-08-28 10:00"`

---

## 四、重要注意事项

1. **cookie 失效**：各平台 cookie 有有效期，失效后用 `account_check` 巡检、重新 `account_bind` 扫码。
2. **定时发布依赖本机常驻**：用 MCP 的 `schedule_task` 或 `sau ... --schedule` 时，
   到点执行的进程需要在运行。个人电脑休眠/关机时定时会停；长期定时建议跑在常驻服务器（如 VPS）。
3. **抖音定时已修复**：旧版会因 Semi 输入掩码把定时时间偏 +2 小时，本包已改为 `.fill()` 原子写入并回读校验，
   发布前可用 `SAU_DRY_RUN_SCHEDULE=1 sau douyin upload-video ...` 干跑校验（不发真实视频）。
4. **不支持撤回/删除**：上游项目只管发，不管删。已发内容需去各平台创作者中心手动删。
5. **不要提交敏感文件**：`conf.py`、`cookies/`、`mcp_data/` 含本机配置与登录态，已写入 `.gitignore`，不要进 git / 不要发给别人。

---

## 五、目录结构

```
social-auto-upload/
├── setup_env.py          # 一键装环境（跨平台）
├── conf.example.py       # conf.py 模板（部署时复制成 conf.py）
├── mcp_server/server.py  # MCP Server（跨平台，从解释器自动推导 sau 路径）
├── uploader/             # 各平台发布器（抖音定时 bug 已修复）
├── sau_cli.py            # sau CLI 入口
├── cookies/              # 本机账号 cookie（不导出）
├── mcp_data/             # 任务队列/状态（不导出）
└── videos/               # 测试素材
```
