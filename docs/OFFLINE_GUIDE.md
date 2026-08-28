# 离线包使用说明（解压即用版）

> 本指南适用于 `social-auto-upload-离线版-mac-arm64.tar.gz`（macOS Apple Silicon）。
> 目标：**解压后无需安装 Python / 依赖 / 浏览器，直接使用**。
> Windows 版（zip）流程相同，仅 MCP 配置路径不同（见文末）。

---

## 零、你不需要做什么（已内置）

| 组件 | 说明 |
|---|---|
| Python 3.12 | 已内置在 `.runtime-python/`（arm64 完整运行时） |
| Python 依赖 | 已装在 `.venv/`（playwright / patchright / fastmcp / flask 等） |
| Chromium 浏览器 | 已内置在 `.playwright-browsers/`（含 headless + ffmpeg） |
| B 站 biliup 工具 | 已内置在 `tools/biliup/`（B 站发布无需联网下载） |

⚠️ 唯一需要你做的系统操作：**首次解压后剥离 macOS 隔离标记**（见下方第一步），否则会报动态库加载失败。

---

## 一、解压并剥离隔离标记（macOS 必做）

压缩包经网盘/微信/邮件传输后带 `com.apple.quarantine` 属性，macOS 解压时会把它传播给所有文件，
导致 numpy 等动态库被系统策略拒绝加载（报 `ImportError: library load disallowed by system policy`）、
Chromium 启动即被强制结束。**务必按下面任一方式处理一次**：

方式 A（推荐，终端一条命令）：

```bash
cd ~/Downloads            # 或你解压的位置
tar -xzf social-auto-upload-离线版-mac-arm64.tar.gz
xattr -cr social-auto-upload
```

方式 B：Finder 双击解压后，打开终端执行：

```bash
xattr -cr ~/Downloads/social-auto-upload
```

> `xattr -cr` 只影响安全属性，不删除任何文件。

---

## 二、验证环境（自检 CLI 与浏览器）

```bash
cd social-auto-upload
./验证环境.command        # 或在 Finder 中双击
```

脚本会自动：
1. 再次剥离隔离标记（防御性）
2. 自检 `sau --help`（应看到 douyin/xiaohongshu/bilibili/tencent 等 12 个平台）
3. 自检 Chromium headless 能否启动
4. 打印 MCP 配置参考（可直接复制）

> 若双击被系统拦截（"无法验证开发者"）：右键 →「打开」，或在终端里执行上面命令均可。

---

## 三、接入 WorkBuddy MCP

编辑 `~/.workbuddy/mcp.json`（没有就新建），在 `mcpServers` 中加入（**路径换成你的实际绝对路径**）：

```json
{
  "mcpServers": {
    "social-auto-upload": {
      "command": "/绝对路径/social-auto-upload/.venv/bin/python3.12",
      "args": ["/绝对路径/social-auto-upload/mcp_server/server.py"],
      "env": { "MCP_TRANSPORT": "stdio" },
      "disabled": false
    }
  }
}
```

重启 WorkBuddy，连接器列表确认 `social-auto-upload` 为 connected。

> Windows 版：`command` 改为 `C:/绝对路径/social-auto-upload/.venv/Scripts/python.exe`。

---

## 四、首次绑定账号（每台机器、每个平台各一次）

账号 cookie 与**设备绑定**，不能复制他人 cookie，必须在**本机**扫码登录。

| 平台 | 登录方式 | 命令 |
|---|---|---|
| 抖音 | 弹浏览器扫码 | `./.venv/bin/sau douyin login --account 账号名 --headed` |
| 小红书 | 弹浏览器扫码 | `./.venv/bin/sau xiaohongshu login --account 账号名 --headed` |
| 快手 | 弹浏览器扫码 | `./.venv/bin/sau kuaishou login --account 账号名 --headed` |
| 多多视频 | 弹浏览器扫码 | `./.venv/bin/sau pinduoduo login --account 账号名 --headed` |
| 搜狐号 | 弹浏览器扫码 | `./.venv/bin/sau sohu login --account 账号名 --headed` |
| 视频号 | 弹浏览器扫码 | `./.venv/bin/sau tencent login --account 账号名 --headed` |
| 微博 | 弹浏览器扫码 | `./.venv/bin/sau weibo login --account 账号名 --headed` |
| **B站** | **终端二维码（不加 --headed）** | `./.venv/bin/sau bilibili login --account 账号名` |

B 站特殊说明：
- 登录走包内 biliup 工具，生成**终端二维码**（用 B 站 App「扫一扫」）；
- 二维码显示不全时，用手机扫项目根目录的 `qrcode.png`；
- 必须在**本地交互式终端**运行（远程 SSH / 无终端环境不行）。

---

## 五、日常使用

### CLI 方式

```bash
# 发布视频
./.venv/bin/sau douyin upload-video --account 账号名 --file videos/demo.mp4 --title "标题" --desc "简介"

# 定时发布（需本机保持运行）
./.venv/bin/sau douyin upload-video --account 账号名 --file videos/demo.mp4 --title "标题" --schedule "2026-09-01 09:00"
```

### WorkBuddy 对话方式（推荐）

绑定 MCP 后直接说人话，例如：
- "绑定抖音账号 矩阵号A"
- "把 /路径/demo.mp4 发到抖音『奇人匠心』，标题『夏日穿搭』，明早 9 点发"
- "上次定时任务都成功了吗？"

---

## 六、常见问题（FAQ）

**Q1：运行时报 `ImportError: ... library load disallowed by system policy`（numpy/cv2）**
→ 隔离标记未剥离。执行 `xattr -cr social-auto-upload` 后重试。

**Q2：Chromium 启动即崩溃 / 进程被杀（SIGKILL）**
→ 同上，对 `.playwright-browsers` 执行 `xattr -cr social-auto-upload/.playwright-browsers`。

**Q3：双击「验证环境.command」被拦截**
→ 右键 →「打开」；或终端执行 `./验证环境.command`。

**Q4：WorkBuddy 里连接器显示未连接 / 报 command not found**
→ mcp.json 的 `command` 必须是**绝对路径**且指向 `.venv/bin/python3.12`（不是系统 python，不是相对路径）；
→ 若提示无法执行，先确认已按第一步剥离隔离标记。

**Q5：B 站登录/上传失败，提示下载 biliup**
→ 确认包内 `tools/biliup/` 存在（本包已内置，无需联网）。若确实缺失，会尝试从 GitHub 下载，国内网络可能超时，重试或配置代理。

**Q6：定时任务到点没发**
→ 定时依赖**本机进程常驻**：运行发布命令的终端/MCP 会话不能关闭，电脑休眠或关机期间定时会停。长期定时建议部署到常驻服务器。

**Q7：cookie 失效了**
→ 各平台 cookie 有有效期，失效后用 `sau <平台> check --account 账号名` 巡检，重新 `login` 扫码即可。

---

## 七、目录布局说明（请勿改动相对结构）

```
social-auto-upload/
├── .venv/                  # 虚拟环境（python 符号链接指向 ../.runtime-python，勿单独移动）
├── .runtime-python/        # Python 3.12 运行时（勿单独移动）
├── .playwright-browsers/   # Chromium 浏览器（勿单独移动）
├── tools/biliup/           # B 站 biliup 工具
├── .venv/bin/sau           # CLI 启动器（自包含）
├── 验证环境.command         # 环境自检脚本
└── mcp_server/server.py    # MCP 服务入口
```

> `.venv`、`.runtime-python`、`.playwright-browsers` 三者是相对引用的，**只能整体移动，不能拆开**。

---

## 八、安全提示

- `cookies/`、`mcp_data/`、`conf.py` 含本机配置与登录态，**不要提交到 Git 或发给他人**；
- 不要随意删除 `cookies/` 目录下的文件，否则需重新扫码；
- 本包不含任何他人账号数据，登录态均为你本机扫码生成。
