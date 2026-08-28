# social-auto-upload MCP 封装 · 使用说明

把 `dreammis/social-auto-upload` 封装成 MCP Server，让 WorkBuddy Agent 能用自然语言
驱动「多平台、多账号」的短视频/图文自动发布与定时任务。

## 已完成的封装

| 层 | 内容 |
|----|------|
| 执行引擎 | 原版 `sau` CLI（已克隆 + 隔离 venv 安装，Python 3.12） |
| MCP Server | `social-auto-upload/mcp_server/server.py`（FastMCP 3.4.7，stdio） |
| WorkBuddy 接入 | 已合并到 `~/.workbuddy/mcp.json` 的 `social-auto-upload` 条目 |
| 7 个工具 | account_bind / account_list / account_check / publish_video / publish_note / schedule_task / task_status |

## 支持的 11 个平台

douyin(抖音) kuaishou(快手) xiaohongshu(小红书) bilibili(B站) tencent(视频号)
baijiahao(百家号) alipay(支付宝生活号) weibo(微博) hupu(虎扑) youtube(YouTube) tiktok(TikTok)

图文仅抖音/快手/小红书支持；定时发布由 MCP 内置 SQLite 调度器（每30s扫描）到点执行。

## 在 WorkBuddy 里怎么用（自然语言示例）

直接跟 Agent 说话即可，Agent 会映射成工具调用：

- "帮我把抖音账号『品牌号』绑一下" → `account_bind(douyin, 品牌号)`
- "现在绑了哪些账号？" → `account_list()`
- "把 /Users/stevebi/视频/demo.mp4 发到抖音『品牌号』和『矩阵A』，标题『夏日穿搭』，话题 穿搭/夏日，明早9点发" → `publish_video(douyin, [品牌号,矩阵A], file, title, tags, schedule_at=2026-08-25T09:00)`
- "发一条小红书图文到『种草号』，图片用 a.png b.png，标题『好物推荐』，正文『……』" → `publish_note(xiaohongshu, [种草号], images, title, note)`
- "上次那几个定时任务都发成功了吗？" → `task_status()`

## 首次使用必做（一次性）

1. **重启 WorkBuddy / 重载 MCP**：让 `social-auto-upload` server 生效（在连接器/自定义连接器里确认状态为 connected）。
2. **绑定账号需本机扫码**：`account_bind` 会在本机弹浏览器或生成二维码。
   - 抖音等触发短信验证时，把验证码写入项目根目录 `verify_code.txt` 即可自动续上。
   - 建议使用 `headless=False`（Mac 本机看得到扫码页）。
3. **chromium 浏览器**：MCP 依赖 playwright chromium，首次需下载（本环境已在后台拉取）。

## 目录与文件

- 项目根：`/Users/stevebi/WorkBuddy/2026-08-24-11-40-00/social-auto-upload/`
- MCP 服务：`mcp_server/server.py`
- 账号/任务状态：`mcp_data/`（已 gitignore，含 cookie 与 tasks.db，**请勿提交**）
- venv：`~/.workbuddy/binaries/python/envs/sau/`

## 已知限制 / 注意事项

- ⚠️ 浏览器自动化平台（小红书/快手/视频号/百家号等）依赖 playwright，平台改版可能导致上传失败，需关注上游更新。
- ⚠️ 定时发布依赖 MCP Server 常驻运行；**Mac 休眠/关机时调度器会停**。长期定时建议迁到常驻 VPS。
- ⚠️ cookie 有效期各平台不同（数天~数十天），`account_check` 可定期巡检，失效需重新 `account_bind`。
- 抖音视频发布触发短信二次验证时会阻塞，需人工填 `verify_code.txt`。
- `mcp_data/`、`conf.py`、`verify_code.txt`、`qrcode.png` 已加入 .gitignore，避免凭据泄露。

## 端到端冒烟（待本机执行）

1. 确认 chromium 下载完成（`python -m playwright install chromium`）。
2. 重启 WorkBuddy 让 MCP 生效。
3. 对话中："绑定抖音账号 test"，按提示扫码。
4. "用 test 账号发一段测试视频"，确认发布成功。
5. "明早9点用 test 发同一段视频"，用 `task_status` 次日确认定时生效。
