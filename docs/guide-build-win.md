# Windows 离线包构建指南（给 Windows 同事）

> 用途：在一台 Windows 机器上生成「解压即用」的 Windows 版 social-auto-upload 离线包，给其他 Windows 同事使用。
> 预计耗时：5~15 分钟（下载依赖 + 浏览器）。**只需要你这一台机器装 Python，其他同事不用装。**

---

## 一、你会收到两个文件

| 文件 | 说明 |
|---|---|
| `social-auto-upload-export.zip` | 项目源码 + 构建脚本（`build_offline_pkg.py`） |
| 本文档 | 操作说明 |

---

## 二、构建步骤

### 1. 安装 Python 3.12（如未装）
- 打开 https://www.python.org/downloads/release/python-3120/ 下载 **Windows installer (64-bit)**
- 安装时 **务必勾选 "Add python.exe to PATH"**，然后 Install Now
- 验证：打开 CMD 运行 `python --version`，应显示 `Python 3.12.x`

### 2. 解压源码
- 把 `social-auto-upload-export.zip` 解压到一个目录，例如 `D:\sau`（路径别带中文和空格）
- 解压后确认里面有 `build_offline_pkg.py` 和 `sau_cli.py`

### 3. 运行构建脚本
在解压目录打开 CMD（地址栏输入 cmd 回车），运行：
```cmd
python build_offline_pkg.py
```

脚本会自动完成（全程有中文进度提示）：
1. 创建 `.venv` 虚拟环境
2. 安装依赖（pip 走清华源，sau + playwright + fastmcp + 各平台发布器）
3. 下载浏览器（chromium-1208，走 npmmirror 国内镜像）
4. 注入环境配置
5. 打包

### 4. 拿到产物
完成后在 `dist\` 目录下出现：
```
social-auto-upload-离线版-win-x86_64.zip   ← 这就是要分发的 Windows 离线包
```

### 5. 自己先验证（可选但推荐）
```cmd
rem 解压到测试目录
tar -xf dist\social-auto-upload-离线版-win-x86_64.zip
cd social-auto-upload
rem 双击「验证环境.bat」，应显示 sau 命令列表 = 成功
```

---

## 三、给其他 Windows 同事的使用说明（分发时附上）

同事收到 `social-auto-upload-离线版-win-x86_64.zip` 后：
1. 解压（Win11 右键「全部解压缩」；Win10 用 7-Zip/自带解压）
2. 进入 `social-auto-upload` 文件夹，双击 **`验证环境.bat`**（确认环境 OK）
3. 绑定账号：`.\venv\Scripts\sau.exe douyin login --account 账号名 --headed`（B站是 `sau bilibili login --account 账号名`，终端二维码）
4. 发布：`.\venv\Scripts\sau.exe douyin upload-video --account 账号名 --file 视频路径 --title "标题" --schedule "2026-08-28 10:00"`
5. 接入 WorkBuddy：mcp.json 指向 `路径\.venv\Scripts\python.exe` + `路径\mcp_server\server.py`

**同事不需要装 Python、不需要装依赖、不需要挂代理。**

---

## 四、常见问题排查

| 问题 | 解决 |
|---|---|
| `python` 不是内部或外部命令 | Python 没加 PATH，重装勾选 "Add to PATH"；或改用 `py -3.12 build_offline_pkg.py` |
| 下载依赖很慢 | 脚本默认走清华源；还慢就挂代理后重跑 |
| 浏览器下载失败（404/超时） | ① 挂代理重跑：`set PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.playwright.dev/dbazure/download/playwright` 再跑 ② 或换官方源后重跑 |
| 杀毒软件拦截 | 添加信任（脚本会下载可执行文件） |
| 构建中途报错 | 把终端最后 20 行报错发给技术 |
