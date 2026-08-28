# 发布操作指南

> 用 social-auto-upload 发布视频/图文（含定时发布）的完整操作说明。
> 前置：已按《首次部署指南》装好环境，并按《账号绑定与多账号管理指南》绑定账号。

---

## 一、命令行发布（各平台参数速查）

### 通用参数
| 参数 | 说明 | 必填 |
|---|---|---|
| `--account` | 账号名（绑定时的名字） | ✅ |
| `--file` | 视频文件路径 | ✅ |
| `--title` | 标题 | ✅ |
| `--desc` | 简介/描述 | 部分平台 |
| `--tags` | 标签，逗号分隔（如 `穿搭,夏日`） | 否 |
| `--thumbnail` | 封面图路径 | 否 |
| `--schedule` | 定时发布 `"YYYY-MM-DD HH:MM"` | 否 |

### 各平台差异（⚠️ 必填参数不同）

| 平台 | 必填 | 差异说明 |
|---|---|---|
| 抖音 | `--title --file` | 定时 `--schedule` |
| 小红书 | `--title --file` | 图文/视频均可；定时 `--schedule` |
| 快手 | `--title --file` | 定时 `--schedule` |
| 多多视频 | `--title --file` | 封面/内容声明/标签自动处理；定时 `--schedule` |
| 搜狐号 | `--title --file` | 定时 `--schedule` |
| 视频号 | `--title --file` | 定时 `--schedule` |
| 微博 | `--title --file` | 定时 `--schedule` |
| **B站** | **`--title --file --desc --tid`** | 简介+分区必填；上传走 API 无需浏览器；定时 `--schedule` |

**B站 `--tid` 常用分区**：`160`=知识、`138`=生活、`65`=科技、`155`=音乐、`129`=动画、`17`=单机游戏。

### 示例
```bash
# 抖音（macOS/Linux）
.venv/bin/sau douyin upload-video \
  --account 奇人匠心 --file videos/demo15s.mp4 \
  --title "测试视频" --desc "测试" --tags "测试,自动化"

# B站（注意 --tid）
.venv/bin/sau bilibili upload-video \
  --account 奇人匠心 --file videos/demo15s.mp4 \
  --title "测试视频" --desc "测试" --tid 160 --tags "测试" \
  --schedule "2026-08-28 10:00"

# Windows 把 .venv/bin/sau 换成 .venv\Scripts\sau
```

---

## 二、定时发布

```bash
--schedule "2026-08-28 10:00"    # 明天上午 10 点
```

- 格式：`YYYY-MM-DD HH:MM`（24 小时制）。
- **各平台定时机制不同**：
  - 抖音/小红书/多多视频/搜狐号/视频号等：脚本在浏览器里选「定时发布」+ 填时间（页面原生定时）。
  - B站：biliup 调 B站 API 的定时接口（`--dtime`）。
- 定时任务**依赖本机常驻**：到点执行时需要进程在运行；电脑休眠/关机定时会停。长期定时建议跑在常驻服务器。

### 定时验证（干跑，不发真实视频）
```bash
SAU_DRY_RUN_SCHEDULE=1 .venv/bin/sau douyin upload-video ... --schedule "2026-08-28 10:00"
```

---

## 三、对话驱动（WorkBuddy 里说人话）

接入 MCP 后直接说：
- "把 /路径/demo.mp4 发到抖音『奇人匠心』，标题『夏日穿搭』，话题 穿搭/夏日，明早9点发"
- "绑定小红书账号 矩阵号A"
- "上次定时任务都成功了吗？"

MCP 提供工具：`account_bind` / `account_list` / `account_check` / `publish_video` / `publish_note` / `schedule_task` / `task_status`。

---

## 四、发布成功判据（各平台不同，别只看日志）

| 平台 | 成功标志 |
|---|---|
| 抖音/小红书/快手等 | 页面出现「发布成功」提示 |
| **多多视频（定时）** | 页面**跳转到「作品管理」页**（视频进入审核中）——定时任务没有 toast |
| **B站** | 命令返回 `submitted` + 无报错；后台「内容管理→定时发布」可见 |

发布后建议到各平台创作者中心确认：定时任务在「定时发布」列表、立即发布在「已发布/作品管理」列表。

---

## 五、常见问题

| 问题 | 解决 |
|---|---|
| 报 cookie 失效 | 重新绑定账号（见账号指南） |
| B站上传报证书错误 | 关代理/VPN 重试（国内直连） |
| 多多视频时间没设上 | 确认先勾选「定时发布」再设时间（脚本已自动处理） |
| 定时到点没发 | 检查本机是否休眠/进程是否在跑 |
| 标签没填上 | 平台话题框是 contenteditable，脚本用键盘输入 `#话题` 处理 |
