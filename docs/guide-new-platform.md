# 新增平台开发指南

> 在 social-auto-upload 上新增一个平台发布器，或修复既有平台的完整工作流。
> 本文是《社交平台发布器开发工作流》的方法论落地，来自多多视频（Playwright 模拟）与 B站（biliup API）两类平台的实际开发经验。

---

## 一、开发流程总览

```
① 先摸清平台机制 → ② 借鉴已有平台 → ③ 真实 DOM 驱动写代码 → ④ 反检测
→ ⑤ 自测迭代闭环 → ⑥ 收尾：清调试代码 + 更新 EXPORT.md + 打包
```

**两个关键前置判断**：
| 平台类型 | 例子 | 实现方式 |
|---|---|---|
| 浏览器模拟 | 抖音/多多视频/搜狐号/视频号 | Playwright + stealth + cookie |
| API 直传 | B站 | biliup CLI（`uploader/<平台>_uploader/runtime.py` 包装） |

先确认目标平台是网页上传还是开放 API——**有官方 API 优先走 API**（稳定、免风控），没有才做浏览器模拟。

---

## 二、借鉴已有平台

| 参考平台 | 文件 | 借鉴点 |
|---|---|---|
| 抖音 | `uploader/douyin_uploader/main.py` | 表单填充、定时、发布判据最全 |
| 多多视频 | `uploader/pinduoduo_uploader/main.py` | React/beast-core 组件、反自动化墙、验证码处理最全 |
| B站 | `uploader/bilibili_uploader/runtime.py` | 外部 CLI 包装（下载二进制 + subprocess 调用） |
| 通用 | `uploader/base_video.py` | 基类、`_msg`/`_save_debug` 工具 |

新平台主类尽量继承 `BaseVideo`，复用 cookie 校验、视频校验、调试快照等通用能力。

---

## 三、真实 DOM 驱动（浏览器模拟类）

### 铁律：禁止猜 DOM，先 dump 真实结构
```python
# 关键节点后落盘快照，读真实结构再写选择器
await _save_debug(page, "step_name")   # 生成 logs/<平台>_debug/<时间>_step_name.html
```

### 选择器优先级
1. `data-testid`（最稳定，如 `beast-core-datePicker-htmlInput`）
2. `[class*="前缀"]`（class 带 hash 后缀必须前缀匹配，如 `[class*="ContentDeclaration_title"]`）
3. `nth(index)` 按位置（列表项顺序即数值时最稳，如时间 `li` 00..59）
4. 文本匹配（`startsWith` 优先于 `includes`，排除条件慎用——副标题可能含关键词）

### 真实点击，禁用 dispatchEvent
React/组件库用合成事件，`el.dispatchEvent(new MouseEvent('click'))` **不触发 onClick**。
```python
# ✅ 真实鼠标点击
r = element.getBoundingClientRect()
await page.mouse.click(r.x + r.width/2, r.y + r.height/2)
# 或
await locator.click(force=True)
```

### 每步回读验证，不"点了就算成功"
```python
# 选完下拉后回读 input.value；勾完 radio 后查 data-checked；设完时间后回读控件值
```

### Playwright 传参陷阱
```python
await page.evaluate(js, [a, b])   # ✅ 多参数包成 list，JS 端 (arr) => 解构
await page.evaluate(js, a, b)     # ❌ 报 "takes 2-3 positional arguments but 4"
```

---

## 四、反检测手段（每平台都要带）

1. **stealth context**：`_stealth_context`（隐藏 WebDriver/navigator 指纹）。
2. **cookie 持久化**：进入发布页 = 安全验证通过，立即 `context.storage_state(path=account_file)` 保存。
3. **首次登录 `--headed`** 人工扫码；后续 cookie 有效可无头。
4. **真实交互节奏**：`wait_for_timeout` 模拟人类停顿，不瞬间连点。
5. **验证码处理**：
   - 滑块拼图：模板匹配（`_find_gap_position` + `_solve_slider`）。
   - 点字/图片验证：OCR 复杂场景准确率低，性价比低。
   - **签名墙/风控**（如 PDD 上传接口恒返 48143）：后端反自动化无法绕过，正路是官方开放平台 API。

---

## 五、自测迭代闭环（自己跑自己修）

```bash
# 1. 自己跑（cookie 有效时；首次扫码才让用户）
~/.workbuddy/binaries/python/envs/sau/bin/sau <平台> upload-video \
  --account 账号 --file videos/demo15s.mp4 \
  --title "测试" --desc "测试" --schedule "2026-08-28 10:00" --headed

# 2. 读日志定位（全程扫描关键节点，不只看最后一行）
#    logs/<平台>.log  +  logs/<平台>_debug/*.html

# 3. 修代码 → 语法检查 → 再跑
~/.workbuddy/binaries/python/envs/sau/bin/python -c \
  "import ast; ast.parse(open('uploader/<平台>_uploader/main.py').read())"
```

**只有这两种情况才让用户介入**：首次扫码登录、手机验证码/需人工判断的验证码。

**⚠️ 环境代理坑**：我的运行环境带 `HTTP_PROXY=127.0.0.1:51285`（WorkBuddy 内部代理），会拦截 biliup 等原生 CLI 的请求导致证书错误。跑 API 直传类命令先移除代理：
```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy sau bilibili upload-video ...
```
（浏览器模拟的 Playwright 不受影响；B站等国内平台直连即可。）

---

## 六、踩坑清单（多多视频 40+ 轮迭代的血泪经验）

| 坑 | 表现 | 正确做法 |
|---|---|---|
| React 合成事件 | 日志"已点"但页面没变 | `page.mouse.click` 真实点击 + 回读 |
| 定时 radio | 点 `input[type=radio]` 无效 | 点 `label[data-testid="beast-core-radio"]` 本身；选中标志 `data-checked` |
| 日期控件 | 无 `datetime-local`，是 readonly text | 点 `input[data-testid="beast-core-datePicker-htmlInput"]` 弹日历；格子排除 disabled/outOfMonth |
| 时间选择 | `filter(has_text=)` 在 portal 超时 | `li` 用 `.nth(index)`（顺序即数值） |
| 弹框类型 | portal 与 modal 混淆 | dump 判类型：`beast-core-portal`（popover/dropdown）vs `[class*=modal]`/`[role=dialog]` |
| 封面候选帧 | 不是 `<img>` 是 `<video>`/canvas | 兜底全页面找 video（排除主视频），等 loading 消失 |
| 成功判据 | 定时任务无 toast | 判「页面跳转作品管理页 / 出现审核中」 |
| Python 正则转义 | `\s`/`\d` SyntaxWarning | JS 模板用 `r"""..."""` |

---

## 七、收尾（每次完成都做）

1. **移除调试 dump**：封面/日历/时间面板 dump、轮询进度日志；保留 `_save_debug`（错误快照）。
2. **清理临时文件**：`logs/<平台>_debug/`、测试视频、`__pycache__`。
3. **注册平台**：确保 `sau_cli.py`（CLI 命令）和 `mcp_server/server.py`（MCP 工具）都注册了新平台。
4. **更新文档**：
   - `EXPORT.md`：登录方式表 + 发布参数速查表加新平台。
   - 本文档的「借鉴平台」表。
5. **重新打包**：
```bash
cd ~/WorkBuddy/2026-08-24-11-40-00/
find social-auto-upload -name __pycache__ -exec rm -rf {} + 2>/dev/null
zip -r social-auto-upload-export.zip social-auto-upload \
  -x "social-auto-upload/.git/*" "social-auto-upload/cookies/*" "social-auto-upload/conf.py" \
     "social-auto-upload/mcp_data/*" "social-auto-upload/.venv/*" \
     "social-auto-upload/social_auto_upload.egg-info/*" "social-auto-upload/logs/*" \
     "social-auto-upload/.DS_Store" "social-auto-upload/**/.DS_Store" "social-auto-upload/*/__pycache__/*"
```
