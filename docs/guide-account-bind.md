# 账号绑定与多账号管理指南

> 适用：social-auto-upload 各平台账号的登录绑定、cookie 维护、多账号管理。
> 所有登录必须**在本机**完成（cookie 与设备绑定，无法复制别人的）。

---

## 一、登录方式总览（⚠️ 分两类）

| 平台 | 登录方式 | 绑定命令 |
|---|---|---|
| 抖音 | 弹浏览器扫码 | `sau douyin login --account 账号名 --headed` |
| 小红书 | 弹浏览器扫码 | `sau xiaohongshu login --account 账号名 --headed` |
| 快手 | 弹浏览器扫码 | `sau kuaishou login --account 账号名 --headed` |
| 多多视频 | 弹浏览器扫码 | `sau pinduoduo login --account 账号名 --headed` |
| 搜狐号 | 弹浏览器扫码 | `sau sohu login --account 账号名 --headed` |
| 视频号 | 弹浏览器扫码 | `sau tencent login --account 账号名 --headed` |
| 微博 | 弹浏览器扫码 | `sau weibo login --account 账号名 --headed` |
| **B站** | **终端二维码（不是浏览器！）** | `sau bilibili login --account 账号名` |

> 命令在项目根目录终端运行；Windows 用 `.venv\Scripts\sau`，macOS/Linux 用 `.venv/bin/sau`。

---

## 二、弹浏览器扫码平台（抖音/小红书/快手/多多视频/搜狐号/视频号/微博）

### 步骤
1. 项目根目录运行绑定命令（带 `--headed`）。
2. 浏览器自动弹出对应平台登录页（首次会加载一会儿）。
3. 在网页上**扫码/账号密码登录**；部分平台要求完成滑块/验证码。
4. 登录成功 = 自动进入发布页，脚本立即保存 cookie。
5. 看到日志提示 cookie 已保存，即可关闭浏览器。

### 注意事项
- 登录页弹出后如果长时间空白，检查网络（国内平台无需代理，**关闭代理/VPN 再试**）。
- 多多视频登录页偶发滑块拼图验证码，脚本会自动尝试拖动；失败则人工拖一次。
- 首次登录成功后 `cookies/` 目录会生成 `<平台>_<账号名>.json`。

---

## 三、B站登录（特殊：终端二维码）

B站不走浏览器，走 **biliup 工具**（首次运行自动下载约 25M 二进制）。

### 步骤
1. 项目根目录运行：`sau bilibili login --account 账号名`（**不要加 --headed**）。
2. **必须在你自己的本地交互式终端**运行（远程 SSH/CI/无终端界面不行）。
3. 终端会渲染 ASCII 二维码；**若显示不全/乱码，打开项目目录下的 `qrcode.png`** 用手机扫。
4. 用 **B站 App「扫一扫」** 扫码确认登录。
5. 成功后 cookie 存到 `cookies/bilibili_账号名.json`。

### 常见问题
| 现象 | 原因 | 解决 |
|---|---|---|
| 命令无反应 | 首次在下载 biliup 二进制（25M） | 等 1-2 分钟，重跑 |
| 终端二维码乱码 | 终端宽度不足/不支持字符 | 打开 `qrcode.png` 扫码 |
| 提示需交互式终端 | 在非交互环境运行（如远程/CI） | 换本地终端 |

---

## 四、多账号管理

- **账号名是唯一标识**：每个账号绑定/发布时都用 `--account 账号名` 区分。
- 同一平台可绑多个账号：`sau douyin login --account 矩阵号A`、`sau douyin login --account 矩阵号B` → 各自独立 cookie。
- 发布时指定账号：`sau douyin upload-video --account 矩阵号A ...`。
- cookie 文件：`cookies/<平台>_<账号名>.json`，**不要手动改/删**（含登录态）。
- 查看已绑账号：`sau <平台> list`（或 WorkBuddy 对话里 `account_list`）。

---

## 五、cookie 失效处理

各平台 cookie 有有效期，失效后发布会报错（如「cookie 失效/请重新登录」）。

### 巡检
```bash
# 逐个平台检查（返回 ok = 有效）
sau douyin check --account 账号名
sau pinduoduo check --account 账号名
sau bilibili check --account 账号名
# ...
```

### 重新绑定
失效账号**重新跑一次绑定命令**即可（扫码刷新 cookie），流程同上文。

> 在 WorkBuddy 对话里也可以：说「绑定抖音账号 XX」或「检查所有账号」让我执行 `account_bind` / `account_check`。
