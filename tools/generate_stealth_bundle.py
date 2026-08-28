#!/usr/bin/env python3
"""从 playwright-stealth 2.0.3 生成新版 utils/stealth.min.js（适配 macOS + 中文环境）。"""
import json
from pathlib import Path

BASE = Path("/tmp/stealth_pkg/extracted/playwright_stealth/js")
OUT = Path("/Users/stevebi/WorkBuddy/2026-08-24-11-40-00/social-auto-upload/utils/stealth.min.js")

def f(name: str) -> str:
    return (BASE / name).read_text(encoding="utf-8")

SCRIPTS = {
    "utils": f("utils.js"),
    "generate_magic_arrays": f("generate.magic.arrays.js"),
    "chrome_app": f("evasions/chrome.app.js"),
    "chrome_csi": f("evasions/chrome.csi.js"),
    "chrome_hairline": f("evasions/chrome.hairline.js"),
    "chrome_load_times": f("evasions/chrome.load.times.js"),
    "iframe_content_window": f("evasions/iframe.contentWindow.js"),
    "media_codecs": f("evasions/media.codecs.js"),
    "navigator_languages": f("evasions/navigator.languages.js"),
    "navigator_permissions": f("evasions/navigator.permissions.js"),
    "navigator_platform": f("evasions/navigator.platform.js"),
    "navigator_plugins": f("evasions/navigator.plugins.js"),
    "navigator_user_agent": f("evasions/navigator.userAgent.js"),
    "navigator_user_agent_data": f("evasions/navigator.userAgentData.js"),
    "navigator_vendor": f("evasions/navigator.vendor.js"),
    "navigator_webdriver": f("evasions/navigator.webdriver.js"),
    "error_prototype": f("evasions/error.prototype.js"),
    "webgl_vendor": f("evasions/webgl.vendor.js"),
}

# 适配 macOS Apple Silicon + 中文账号：平台不伪装成 Win32、语言 zh-CN、并发 8 核
opts = {
    "navigator_hardware_concurrency": 8,
    "navigator_languages_override": ["zh-CN", "zh"],
    "navigator_platform": None,
    "navigator_user_agent": None,
    "navigator_vendor": None,
    "webgl_renderer": None,
    "webgl_vendor": None,
    "script_logging": False,
}

order = [
    "chrome_app", "chrome_csi", "chrome_hairline", "chrome_load_times",
    "iframe_content_window", "media_codecs", "navigator_languages",
    "navigator_permissions", "navigator_platform", "navigator_plugins",
    "navigator_user_agent", "navigator_user_agent_data", "navigator_vendor",
    "navigator_webdriver", "error_prototype", "webgl_vendor",
]

header = (
    "/*!\n"
    " * social-auto-upload stealth bundle\n"
    " * Generated from playwright-stealth 2.0.3 (https://pypi.org/project/playwright-stealth/)\n"
    " * Bundled on 2026-08-28. Adapts: macOS arm64, zh-CN locale.\n"
    " * License: MIT\n"
    " */\n"
)
body = "(() => {\n"
body += "const opts = " + json.dumps(opts, ensure_ascii=False) + ";\n"
body += SCRIPTS["utils"] + "\n"
body += SCRIPTS["generate_magic_arrays"] + "\n"
for key in order:
    body += SCRIPTS[key] + "\n"
body += "})();\n"

OUT.write_text(header + body, encoding="utf-8")
print(f"✅ 已生成: {OUT} ({OUT.stat().st_size/1024:.1f} KB, {len(header+body)} chars)")
