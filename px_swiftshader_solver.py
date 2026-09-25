#!/usr/bin/env python3
"""Path D —— 自建 stealth headless 浏览器 + SwiftShader 软件渲染，真按收割 PerimeterX press `_px3`。

背景（见 px_hold_research.md）：
  - press `_px3` 纯协议不可伪造（服务端加密 + captcha.js 渲染门 captchaNotRendered + WASM PoW 执行证明）。
  - 现实落点是 Path D：真实渲染引擎里真按住，收割 press `_px3` 回喂 risk.py verify#2。
  - 旧“无 GPU”被拦的根因已用 **SwiftShader 软件渲染** 解决（px_swiftshader_probe.py 实测
    WebGL renderer = "ANGLE (... SwiftShader driver)"，readPixels 真出帧）。

对外接口（与 bit_px_solver.harvest 同签名，可直接 drop-in risk.py 的 _solve_via_bitbrowser 分支）：
    harvest(proxy: str | None, *, want_press: bool = True, timeout_s: int = 60) -> dict
    返回 {"px3","pxvid","pxde","pressed"}

关键实现：
  - headless Chromium + SwiftShader：
      --headless=new --use-gl=angle --use-angle=swiftshader --enable-unsafe-swiftshader
      --disable-blink-features=AutomationControlled --no-sandbox
    （macOS 实测生效；see px_swiftshader.log）
  - 真按：CDP Input.dispatchMouseEvent（mouseMoved 逼近 → mousePressed → 按住期间浮点微抖 mouseMoved →
    mouseReleased）。按住时长落在经验区间 1000–3000ms（研究 #B11，真正被校验的 pressDuration 是 1–3s，
    非 8–10s）。pressDuration == pointerup.ts − pointerdown.ts 由真实 wall-clock 保证。坐标全程浮点。
  - 收割：challenge 清除后读 cookie，抽 _px3/_pxvid/_pxde，确认 _px3 含 ":1000:"（已解标记）。
  - 全量日志到 px_solver/px_swiftshader.log。
"""
from __future__ import annotations

import json
import logging
import os
import random
import string
import sys
import time
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_LOG_PATH = os.path.join(_HERE, "px_swiftshader.log")

logger = logging.getLogger("px_swiftshader")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _fh = logging.FileHandler(_LOG_PATH, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_fh)
    _sh = logging.StreamHandler()
    _sh.setFormatter(logging.Formatter("[px_swiftshader] %(message)s"))
    logger.addHandler(_sh)

# ── 配置（env 可覆盖） ────────────────────────────────────────────────
ENGINE = os.environ.get("PX_SWIFTSHADER_ENGINE", "patchright").strip().lower()  # patchright | playwright
HEADFUL = os.environ.get("PX_SWIFTSHADER_HEADFUL", "0").strip() == "1"
# 系统级 HID 鼠标（macOS Quartz），绕过 CDP。必须 headed。见 os_press.py
OS_PRESS = os.environ.get("PX_OS_PRESS", "0").strip() == "1"
# 按住后端：pw_mouse（Playwright page.mouse，不需辅助功能）| cdp | os_hid
# 空 = 旧行为（OS_PRESS 则 os_hid 否则 cdp）
PRESS_BACKEND = os.environ.get("PX_PRESS_BACKEND", "").strip().lower()
BROWSER = os.environ.get("PX_BROWSER", "chromium").strip().lower()  # chromium|firefox|webkit
REAL_GPU = os.environ.get("PX_REAL_GPU", "0").strip() == "1"
HUMAN_SLOW = os.environ.get("PX_HUMAN_SLOW", "0").strip() == "1"
SIGNUP_URL = os.environ.get("PX_SIGNUP_URL", "https://signup.live.com/signup?lic=1")
HOLD_MS_MIN = int(os.environ.get("PX_HOLD_MS_MIN", "1500"))
HOLD_MS_MAX = int(os.environ.get("PX_HOLD_MS_MAX", "2600"))
# 仅用于“主动触发 press 挑战”的实验：故意暴露自动化痕迹以拉高 PX 风险分。
# 默认 0（生产隐身）。=1 时去掉反自动化 flag 并暴露 navigator.webdriver=true。
EXPOSE_AUTOMATION = os.environ.get("PX_EXPOSE_AUTOMATION", "0").strip() == "1"

_UA_WIN = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
_UA_MAC = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")

# UA 必须与 WebGL renderer 自洽，否则是送分的机器人信号：
#   REAL_GPU=1 在 macOS 上 renderer 报 "ANGLE (Apple, ANGLE Metal Renderer: Apple Mx)"，
#   此时若 UA 还自称 Windows，微软注册页首屏直接 "We ran into a problem"（实测 2026-09-01）。
#   故 REAL_GPU + darwin 默认切 macOS UA；PX_UA 可显式覆盖。
UA = os.environ.get("PX_UA", "").strip() or (
    _UA_MAC if (REAL_GPU and sys.platform == "darwin") else _UA_WIN
)

# 浏览器 locale / 时区。默认沿用 en-US，但代理出口在别国时应随出口改，
# 否则 IP 地理与 JS 时区不一致同样是风险信号。
LOCALE = os.environ.get("PX_LOCALE", "en-US").strip() or "en-US"
TIMEZONE = os.environ.get("PX_TIMEZONE", "America/New_York").strip() or "America/New_York"

# macOS 实测生效的 SwiftShader 软件渲染启动参数
SWIFTSHADER_ARGS = [
    "--headless=new",
    "--use-gl=angle",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu-sandbox",
    "--window-size=1280,900",
]


def _parse_proxy(proxy: str):
    """兼容 http://user:pass@host:port 与 host:port:user:pass 两种格式 → playwright proxy dict。"""
    proxy = (proxy or "").strip()
    if not proxy:
        return None
    if "://" in proxy:
        from urllib.parse import urlparse
        u = urlparse(proxy)
        host, port, user, pwd = u.hostname or "", u.port or 0, u.username or "", u.password or ""
    else:
        a = proxy.split(":")
        host, port, user, pwd = a[0], a[1], (a[2] if len(a) > 2 else ""), (":".join(a[3:]) if len(a) > 3 else "")
    d = {"server": f"http://{host}:{port}"}
    if user:
        d["username"] = user
    if pwd:
        d["password"] = pwd
    return d


def _import_sync_playwright():
    if ENGINE == "playwright":
        from playwright.sync_api import sync_playwright  # type: ignore
        return sync_playwright
    try:
        from patchright.sync_api import sync_playwright  # type: ignore
        return sync_playwright
    except Exception as e:  # noqa: BLE001
        logger.warning("patchright 不可用（%s），回退 playwright", e)
        from playwright.sync_api import sync_playwright  # type: ignore
        return sync_playwright


def _find_captcha(page):
    """在所有 frame 里找 #px-captcha，返回 (frame, element, box)。box 为相对主框架视口的坐标（浮点）。

    hsprotect 是 OOPIF 时 Playwright 可能列不出该 frame，此时退回父页 iframe 盒子。
    """
    for fr in page.frames:
        try:
            el = fr.query_selector("#px-captcha")
            if not el:
                continue
            box = el.bounding_box()
            if box and box.get("width", 0) > 2 and box.get("height", 0) > 2:
                return fr, el, box
        except Exception:
            continue
    try:
        for el in page.query_selector_all('iframe[src*="hsprotect"]'):
            box = el.bounding_box()
            if box and box.get("width", 0) > 80 and box.get("height", 0) > 30:
                return page, el, box
    except Exception:
        pass
    return None, None, None


def _collect_px(ctx) -> dict:
    out = {"px3": "", "pxvid": "", "pxde": "", "pxcts": ""}
    try:
        for c in ctx.cookies():
            n = c.get("name")
            if n == "_px3":
                out["px3"] = c.get("value", "")
            elif n == "_pxvid":
                out["pxvid"] = c.get("value", "")
            elif n == "_pxde":
                out["pxde"] = c.get("value", "")
            elif n == "pxcts":
                out["pxcts"] = c.get("value", "")
    except Exception as e:  # noqa: BLE001
        logger.warning("读取 cookie 失败: %s", e)
    return out


def _cdp_move(cdp, x: float, y: float, buttons: int = 0):
    cdp.send("Input.dispatchMouseEvent", {
        "type": "mouseMoved",
        "x": float(x), "y": float(y),
        "button": "left" if buttons else "none",
        "buttons": buttons,
        "pointerType": "mouse",
    })


def _cdp_press_hold(cdp, cx: float, cy: float, hold_ms: int, log) -> float:
    """CDP 真按住：逼近 → press → 按住期间浮点微抖 → release。返回实际按住毫秒（==pressDuration）。"""
    n_approach = 18 if HUMAN_SLOW else 8
    span_x = 140.0 if HUMAN_SLOW else 60.0
    span_y = 70.0 if HUMAN_SLOW else 30.0
    for i in range(n_approach):
        t = (i + 1) / n_approach
        jx = cx - span_x + t * span_x + random.uniform(-2.5, 2.5)
        jy = cy - span_y + t * span_y + random.uniform(-2.5, 2.5)
        _cdp_move(cdp, jx, jy, buttons=0)
        time.sleep(random.uniform(0.03, 0.08) if HUMAN_SLOW else random.uniform(0.02, 0.05))
    _cdp_move(cdp, cx + random.uniform(-0.7, 0.7), cy + random.uniform(-0.7, 0.7), buttons=0)
    time.sleep(random.uniform(0.18, 0.42) if HUMAN_SLOW else random.uniform(0.08, 0.16))

    px = cx + random.uniform(-0.5, 0.5)
    py = cy + random.uniform(-0.5, 0.5)
    t_down = time.time()
    cdp.send("Input.dispatchMouseEvent", {
        "type": "mousePressed", "x": float(px), "y": float(py),
        "button": "left", "buttons": 1, "clickCount": 1,
        "pointerType": "mouse",
    })
    log("mousePressed x=%.3f y=%.3f" % (px, py))

    while (time.time() - t_down) * 1000.0 < hold_ms:
        jx = px + random.uniform(-0.9, 0.9)
        jy = py + random.uniform(-0.7, 0.7)
        _cdp_move(cdp, jx, jy, buttons=1)
        time.sleep(random.uniform(0.07, 0.16) if HUMAN_SLOW else random.uniform(0.05, 0.13))

    t_up = time.time()
    cdp.send("Input.dispatchMouseEvent", {
        "type": "mouseReleased", "x": float(px), "y": float(py),
        "button": "left", "buttons": 0, "clickCount": 1,
        "pointerType": "mouse",
    })
    dur_ms = (t_up - t_down) * 1000.0
    log("mouseReleased x=%.3f y=%.3f pressDuration=%.1fms" % (px, py, dur_ms))
    return dur_ms


def _pw_mouse_press_hold(page, cx: float, cy: float, hold_ms: int, log) -> float:
    """Playwright page.mouse 贝塞尔逼近 + 按住微抖（不走 CDP、不需辅助功能）。"""
    sx = random.uniform(180, 520)
    sy = random.uniform(140, 360)
    steps = random.randint(18, 36) if HUMAN_SLOW else random.randint(12, 22)
    ctrl_x = (sx + cx) / 2 + random.uniform(-90, 90)
    ctrl_y = (sy + cy) / 2 + random.uniform(-70, 70)
    page.mouse.move(sx, sy)
    page.wait_for_timeout(int(random.uniform(80, 220) if not HUMAN_SLOW else random.uniform(200, 500)))
    for i in range(1, steps + 1):
        t = i / steps
        mx = (1 - t) ** 2 * sx + 2 * (1 - t) * t * ctrl_x + t ** 2 * cx + random.uniform(-1.4, 1.4)
        my = (1 - t) ** 2 * sy + 2 * (1 - t) * t * ctrl_y + t ** 2 * cy + random.uniform(-1.4, 1.4)
        page.mouse.move(mx, my)
        page.wait_for_timeout(int(random.uniform(8, 28) if not HUMAN_SLOW else random.uniform(16, 42)))
    page.wait_for_timeout(int(random.uniform(80, 180) if not HUMAN_SLOW else random.uniform(250, 600)))
    px = cx + random.uniform(-0.6, 0.6)
    py = cy + random.uniform(-0.6, 0.6)
    t_down = time.time()
    page.mouse.move(px, py)
    page.mouse.down()
    log("pw.mouse.down x=%.3f y=%.3f" % (px, py))
    while (time.time() - t_down) * 1000.0 < hold_ms:
        page.mouse.move(px + random.uniform(-1.2, 1.2), py + random.uniform(-0.9, 0.9))
        page.wait_for_timeout(int(random.uniform(40, 110)))
    t_up = time.time()
    page.mouse.up()
    dur_ms = (t_up - t_down) * 1000.0
    log("pw.mouse.up pressDuration=%.1fms" % dur_ms)
    return dur_ms


def _pw_locator_press_hold(page, hold_ms: int, log) -> float:
    """对 iframe 内 #px-captcha 用 locator.click(delay=hold) 长按。"""
    target = None
    for fr in page.frames:
        try:
            el = fr.query_selector("#px-captcha")
            if not el:
                continue
            box = el.bounding_box()
            if box and box.get("width", 0) > 30 and box.get("height", 0) > 8:
                target = el
                log("locator 目标 frame=%s box=%s", (fr.url or "")[:60], {k: round(v, 1) for k, v in box.items()})
                break
        except Exception:
            continue
    if target is None:
        raise RuntimeError("locator 找不到可见 #px-captcha")
    try:
        target.hover(timeout=3000)
    except Exception:
        pass
    t0 = time.time()
    target.click(delay=max(800, int(hold_ms)), force=True, timeout=hold_ms + 8000)
    dur = (time.time() - t0) * 1000.0
    log("locator.click delay=%dms wall=%.0fms" % (hold_ms, dur))
    return dur


def _pw_iframe_press_hold(page, hold_ms: int, log) -> float:
    """OOPIF 内 hover（不 force）后 page.mouse.down/up，避免父页坐标打不进 hsprotect iframe。"""
    loc = None
    used = ""
    for sel in (
        'iframe[src*="hsprotect"]',
        'iframe[src*="captcha"]',
        'iframe[src*="px-cdn"]',
        "iframe",
    ):
        try:
            cap = page.frame_locator(sel).first.locator("#px-captcha")
            if cap.count() == 0:
                continue
            first = cap.first
            if first.is_visible():
                loc = first
                used = sel
                break
        except Exception:
            continue
    if loc is None:
        raise RuntimeError("pw_iframe 找不到可见 hsprotect #px-captcha")
    log("pw_iframe 目标 sel=%s", used)
    try:
        loc.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    box = loc.bounding_box() or {}
    cx = (box.get("x") or 0) + (box.get("width") or 0) / 2.0
    cy = (box.get("y") or 0) + (box.get("height") or 0) / 2.0
    loc.hover(timeout=5000, force=False)
    page.wait_for_timeout(int(random.uniform(80, 180) if not HUMAN_SLOW else random.uniform(160, 320)))
    if box:
        page.mouse.move(cx + random.uniform(-1.2, 1.2), cy + random.uniform(-0.8, 0.8))
        page.wait_for_timeout(int(random.uniform(60, 140)))
    t0 = time.time()
    try:
        loc.click(delay=max(800, int(hold_ms)), force=False, timeout=hold_ms + 10000)
        dur = (time.time() - t0) * 1000.0
        log("pw_iframe locator.click(no-force) delay=%dms wall=%.0fms box=%s",
            hold_ms, dur, {k: round(v, 1) for k, v in box.items()} if box else {})
        return dur
    except Exception as e:
        log("pw_iframe click(no-force) 失败，改 mouse.down: %s", e)
    page.mouse.down()
    log("pw_iframe mouse.down after hover cx=%.2f cy=%.2f", cx, cy)
    while (time.time() - t0) * 1000.0 < hold_ms:
        if box:
            page.mouse.move(cx + random.uniform(-1.1, 1.1), cy + random.uniform(-0.7, 0.7))
        page.wait_for_timeout(int(random.uniform(50, 110)))
    page.mouse.up()
    dur = (time.time() - t0) * 1000.0
    log("pw_iframe mouse.up wall=%.0fms" % dur)
    return dur


def _parse_ob_solve(body: str, tag: str = "") -> dict:
    """从 collector/bundle JSON 抽 _px3 / solve_result。tag 缺省时试 Outlook HAR tag。"""
    out = {"solve_result": None, "has_px3": False, "px3": "", "segs": 0, "score": ""}
    try:
        j = json.loads(body)
    except Exception:
        return out
    ob = j.get("ob") or ""
    if not ob:
        out["do"] = j.get("do")
        return out
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    from px_ob_decode import decode_ob  # noqa: WPS433

    tags = [t for t in (tag, "YjIYfyxJHRR9") if t]
    segs = []
    for t in tags:
        try:
            segs = decode_ob(ob, t)
            if segs:
                break
        except Exception:
            continue
    out["segs"] = len(segs)
    for seg in segs:
        parts = seg.split("|")
        if "_px3" in seg:
            out["has_px3"] = True
            for p in parts:
                if ":1000:" in p or p.count(":") >= 2:
                    out["px3"] = p[:80]
            continue
        if "score" in seg:
            out["score"] = seg[:60]
            continue
        # unobpx solve_result：短段，末字段 0 / -1（不要把 score|1|binary 当成解题结果）
        if len(parts) <= 3 and parts[-1] in ("0", "-1") and "px" not in seg.lower():
            out["solve_result"] = parts[-1]
    return out


def _rand_word(n=8):
    return "".join(random.choice(string.ascii_lowercase) for _ in range(n))


def _page_state(page):
    """采集当前可见输入框/按钮/标题，便于自适应驱动 + 排障。"""
    try:
        return page.evaluate(
            """() => {
              const vis = (e) => { const r=e.getBoundingClientRect();
                const s=getComputedStyle(e); return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'; };
              const inputs=[...document.querySelectorAll('input')].filter(vis)
                .map(i=>({id:i.id,name:i.name,type:i.type,ph:i.placeholder}));
              const btns=[...document.querySelectorAll('button,input[type=submit]')].filter(vis)
                .map(b=>({id:b.id,txt:(b.innerText||b.value||'').trim().slice(0,30)}));
              const h=[...document.querySelectorAll('h1,[role=heading],#title')].filter(vis)
                .map(x=>(x.innerText||'').trim().slice(0,60)).filter(Boolean);
              return {inputs, btns, heading:h};
            }"""
        )
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def _click_next(page, log):
    for sel in ('#nextButton', 'button[data-testid="primaryButton"]', '#iSignupAction',
                'button[type="submit"]', 'input[type="submit"]', 'button.win-button'):
        try:
            b = page.query_selector(sel)
            if b and b.is_visible():
                b.click()
                log("点击 Next (sel=%s)" % sel)
                return True
        except Exception:
            continue
    # 兜底：回车
    try:
        page.keyboard.press("Enter")
        log("回车提交（无按钮匹配）")
        return True
    except Exception:
        return False


def _pick_dropdown(page, trigger_sel, log):
    """自定义 Fluent 下拉：点击触发器 → 选一个 option（优先点 role=option，兜底键盘）。"""
    trig = page.query_selector(trigger_sel)
    if not trig or not trig.is_visible():
        # 兜底：也许是原生 <select>
        native = page.query_selector(trigger_sel.replace("Dropdown", "") + ", " + trigger_sel)
        if native:
            try:
                native.select_option(index=random.randint(1, 5))
                log("原生 select %s 选中", trigger_sel)
                return
            except Exception:
                pass
        return
    # label 会拦截 pointer 事件 → 用 force + 短超时；再兜底 JS click
    try:
        trig.click(force=True, timeout=3000)
    except Exception:
        try:
            page.eval_on_selector(trigger_sel, "el => el.click()")
        except Exception:
            pass
    page.wait_for_timeout(400)
    opts = page.query_selector_all('[role="option"], li[role="option"], .ms-Dropdown-item, option')
    opts = [o for o in opts if o.is_visible()]
    if opts:
        pick = random.choice(opts[1:6] if len(opts) > 6 else opts[1:] or opts)
        try:
            pick.click(force=True, timeout=3000)
        except Exception:
            pick.click()
        log("下拉 %s 选中一个 option（共 %d）", trigger_sel, len(opts))
    else:
        # 键盘兜底
        for _ in range(random.randint(1, 4)):
            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(80)
        page.keyboard.press("Enter")
        log("下拉 %s 键盘选择", trigger_sel)
    page.wait_for_timeout(250)


def _visible_captcha_box(page):
    """只认“可见”的 #px-captcha（box 足够大）为真实 press 挑战。
    PerimeterX 会预注入一个隐藏/零尺寸的 #px-captcha 占位符，需用尺寸过滤掉。"""
    _, _, box = _find_captcha(page)
    if box and box.get("width", 0) > 30 and box.get("height", 0) > 20:
        return box
    return None


def _drive_signup(page, log, check_stop):
    """完整驱动 signup（username→password→name→birthdate），每步后检查 #px-captcha。

    check_stop(): 返回 True 表示已出现 #px-captcha 或已拿到目标，提前停止驱动。
    """
    try:
        page.wait_for_timeout(2000)
        log("初始页面状态: %s", json.dumps(_page_state(page), ensure_ascii=False)[:400])

        # Step 1: 用 @outlook.com 全邮箱（避免“格式不对”卡在第一屏）
        uname = _rand_word(7) + str(random.randint(100, 999))
        email = uname + "@outlook.com"
        filled = False
        for sel in ('#usernameInput', 'input[name="MemberName"]', 'input[type="email"]',
                    '#MemberName', 'input[type="text"]'):
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.fill(email)
                log("填邮箱 %s (sel=%s)", email, sel)
                filled = True
                break
        if filled:
            page.wait_for_timeout(400)
            _click_next(page, log)
            page.wait_for_timeout(2500)
        if check_stop():
            return
        log("Step1 后状态: %s", json.dumps(_page_state(page), ensure_ascii=False)[:400])

        # Step 2: 密码
        pwd = "Aa!" + _rand_word(9) + str(random.randint(10, 99))
        for sel in ('#PasswordInput', 'input[name="Password"]', 'input[type="password"]',
                    '#passwordEntry'):
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.fill(pwd)
                log("填密码 (sel=%s)", sel)
                page.wait_for_timeout(300)
                _click_next(page, log)
                page.wait_for_timeout(2500)
                break
        if check_stop():
            return
        log("Step2 后状态: %s", json.dumps(_page_state(page), ensure_ascii=False)[:400])

        # Step 3: 姓名
        try:
            fn = page.query_selector('#firstNameInput, input[name="FirstName"]')
            ln = page.query_selector('#lastNameInput, input[name="LastName"]')
            if fn and fn.is_visible():
                fn.fill(_rand_word(5).capitalize())
                if ln:
                    ln.fill(_rand_word(6).capitalize())
                log("填姓名")
                page.wait_for_timeout(300)
                _click_next(page, log)
                page.wait_for_timeout(2500)
        except Exception as e:  # noqa: BLE001
            log("姓名步异常: %s", e)
        if check_stop():
            return

        # Step 4: 生日（BirthMonth/BirthDay 是自定义下拉，非原生 select；用点击+键盘选择）
        try:
            # 年（原生 number input）
            yr = page.query_selector('#BirthYear, input[name="BirthYear"]')
            if yr and yr.is_visible():
                yr.fill(str(random.randint(1988, 2000)))
            # 月/日：先试原生 select_option，失败则点击+ArrowDown+Enter
            for trig in ('#BirthMonthDropdown', '#BirthDayDropdown'):
                try:
                    _pick_dropdown(page, trig, log)
                except Exception as e:  # noqa: BLE001
                    log("下拉 %s 选择异常: %s", trig, e)
            log("填生日（year+month+day）")
            page.wait_for_timeout(400)
            _click_next(page, log)
            page.wait_for_timeout(3500)
        except Exception as e:  # noqa: BLE001
            log("生日步异常: %s", e)
        if check_stop():
            return
        log("Step4 后状态: %s", json.dumps(_page_state(page), ensure_ascii=False)[:400])

        # Step 5: 推进剩余屏（姓名等），每屏填内容再提交；press 挑战多半在姓名后出现
        for _ in range(4):
            if check_stop():
                return
            st = _page_state(page)
            heading = " ".join(st.get("heading", []))
            log("Step5 循环 heading=%s inputs=%s", heading[:60],
                [i.get("id") or i.get("name") for i in st.get("inputs", [])])
            # 姓名屏
            fn = page.query_selector('#firstNameInput, input[name="firstNameInput"], input[name="FirstName"]')
            ln = page.query_selector('#lastNameInput, input[name="lastNameInput"], input[name="LastName"]')
            if fn and fn.is_visible():
                fn.fill(_rand_word(5).capitalize())
                if ln:
                    ln.fill(_rand_word(6).capitalize())
                log("填姓名（Step5）")
                page.wait_for_timeout(300)
            if not _click_next(page, log):
                break
            page.wait_for_timeout(3800)
            if check_stop():
                return
        log("驱动结束状态: %s", json.dumps(_page_state(page), ensure_ascii=False)[:400])
    except Exception as e:  # noqa: BLE001
        log("drive_signup 异常: %s", e)


def harvest(
    proxy: Optional[str] = None,
    *,
    want_press: bool = True,
    timeout_s: int = 60,
    challenge_url: Optional[str] = None,
    session_id: Optional[str] = None,
    vid: Optional[str] = None,
    uuid: Optional[str] = None,
    app_id: Optional[str] = None,
    preseed_cookies: Optional[list] = None,
    signup_page_url: Optional[str] = None,
) -> dict:
    """Path D 主入口：SwiftShader headless 浏览器真按收割 press `_px3`。

    返回 {"px3","pxvid","pxde","pressed"}，与 bit_px_solver.harvest 同结构。

    两种模式：
      1) 定向模式（传入 challenge_url，recommended path #1）：直接把 SwiftShader 浏览器导航到
         注册会话的 PX challengeUrl（iframe.hsprotect.net/index.html?app_id=..&session_id=<注册>），
         在注册会话的 PX 上下文里真按收割 press `_px3`，使 token 绑定注册 vid/session。
         可选 preseed_cookies：把注册 requests 会话的 _pxvid/_pxhd/pxcts 预置进浏览器，
         令浏览器 _pxvid 与注册 vid 一致。
      2) 独立模式（不传 challenge_url，旧行为，非破坏性回退）：浏览器自开 signup.live.com、
         驱动表单触发 PX、收割。此时 solver 会另起一个 PX 会话（session_id/vid 与注册不同）。
    """
    sync_playwright = _import_sync_playwright()
    proxy_dict = _parse_proxy(proxy) if proxy else None
    res = {"px3": "", "pxvid": "", "pxde": "", "pxcts": "", "pressed": False}
    run_id = time.strftime("%H%M%S")
    targeted = bool(challenge_url)

    def log(msg, *a):
        text = (msg % a) if a else msg
        logger.info("[%s] %s", run_id, text)

    logger.info("=" * 78)
    log("harvest 开始 engine=%s browser=%s headful=%s backend=%s want_press=%s mode=%s proxy=%s",
        ENGINE, BROWSER, HEADFUL, PRESS_BACKEND or ("os_hid" if OS_PRESS else "cdp"),
        want_press, ("定向challengeUrl" if targeted else "独立signup"),
        (str(proxy)[:40] if proxy else "DIRECT(无代理)"))
    if targeted:
        log("定向模式 challengeUrl=%s", challenge_url)
        log("定向模式 注册 session_id=%s vid=%s uuid=%s app_id=%s preseed=%s",
            session_id or "?", vid or "?", uuid or "?", app_id or "?",
            (len(preseed_cookies) if preseed_cookies else 0))
    net_events: list[str] = []
    render_not_rendered = {"hit": False}
    bundle_obs: list[dict] = []
    session_vid = {"cookie": "", "captcha_js": "", "uuid": ""}
    last_bundle_tag = {"tag": ""}

    with sync_playwright() as pw:
        args = list(SWIFTSHADER_ARGS)
        if HEADFUL or REAL_GPU:
            args = [a for a in args if a != "--headless=new" and not a.startswith("--headless")]
            if REAL_GPU:
                skip = {"--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
                        "--disable-gpu-sandbox"}
                args = [a for a in args if a not in skip]
                log("REAL_GPU=1 去掉 SwiftShader，走本机 GPU")
            if HEADFUL:
                log("HEADFUL=1 已去掉 --headless")
            if REAL_GPU:
                if sys.platform == "darwin":
                    args.extend([
                        "--ignore-gpu-blocklist",
                        "--enable-gpu-rasterization",
                        "--enable-features=CanvasOopRasterization,Accelerated2dCanvas,Metal",
                        "--use-angle=metal",
                    ])
                else:
                    args.extend([
                        "--ignore-gpu-blocklist",
                        "--enable-gpu",
                        "--enable-features=Vulkan,DefaultANGLEVulkan,CanvasOopRasterization",
                    ])
        log("launch args=%s", " ".join(args) if args else "(engine default)")
        if OS_PRESS and not HEADFUL:
            log("⚠️ PX_OS_PRESS=1 但 HEADFUL=0：HID 点不到窗口，将仍走 CDP")
        if EXPOSE_AUTOMATION:
            # 实验模式：移除反自动化 flag，让 PX 更可能弹出 press 挑战
            args = [a for a in args if a != "--disable-blink-features=AutomationControlled"]
            args.append("--enable-automation")
            log("⚗️ EXPOSE_AUTOMATION=1（实验：故意暴露自动化以触发 press 挑战）")
        launch_kwargs = {"headless": not HEADFUL, "args": args}
        if proxy_dict:
            launch_kwargs["proxy"] = proxy_dict
        launcher = pw.chromium
        if BROWSER == "firefox":
            launcher = pw.firefox
            launch_kwargs.pop("args", None)
        elif BROWSER == "webkit":
            launcher = pw.webkit
            launch_kwargs.pop("args", None)
        browser = launcher.launch(**launch_kwargs)
        ctx_ua = UA
        if BROWSER == "firefox":
            ctx_ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) "
                      "Gecko/20100101 Firefox/133.0")
        elif BROWSER == "webkit":
            ctx_ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15")
        ctx = browser.new_context(
            user_agent=ctx_ua,
            viewport={"width": 1280, "height": 900},
            locale=LOCALE,
            timezone_id=TIMEZONE,
        )
        log("context ua=%s locale=%s tz=%s", ctx_ua[:60], LOCALE, TIMEZONE)
        page = ctx.new_page()

        # 定向 press：把 collector POST 里的 vid 钉死在 challenge vid，避免 iframe 内二次签发 vid
        if targeted and vid:
            _pin_vid = str(vid)

            def _pin_collector_vid(route):
                req = route.request
                if req.method != "POST":
                    route.continue_()
                    return
                u = req.url
                if "/assets/js/bundle" not in u and "/api/v2/" not in u and "/b/c/" not in u:
                    route.continue_()
                    return
                pd = req.post_data or ""
                if "vid=" not in pd:
                    route.continue_()
                    return
                try:
                    from urllib.parse import parse_qs, urlencode
                    fields = {k: (v[0] if v else "") for k, v in parse_qs(pd, keep_blank_values=True).items()}
                    old = fields.get("vid", "")
                    if old and old != _pin_vid:
                        fields["vid"] = _pin_vid
                        route.continue_(post_data=urlencode(fields))
                        log("route 钉死 collector vid %s → %s", old[:24], _pin_vid[:24])
                        return
                except Exception as e:  # noqa: BLE001
                    log("route vid 重写失败: %s", e)
                route.continue_()

            try:
                page.route("**/*", _pin_collector_vid)
                log("已启用 collector vid 路由钉死 challenge vid=%s", _pin_vid[:36])
            except Exception as e:  # noqa: BLE001
                log("page.route vid 钉死不可用: %s", e)

        # 网络监听：记录 collector POST / captchaNotRendered / 挑战资源
        def on_request(req):
            u = req.url
            if "captcha.js" in u:
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(u).query)
                if q.get("v"):
                    session_vid["captcha_js"] = q["v"][0]
                if q.get("u"):
                    session_vid["uuid"] = q["u"][0]
                log("captcha.js vid=%s uuid=%s", session_vid["captcha_js"][:36], session_vid["uuid"][:36])
            if "/api/v2/collector" in u or "captcha.js" in u or "hsprotect" in u or "clientError" in u:
                tag = req.method + " " + u[:160]
                net_events.append(tag)
                if "captchaNotRendered" in u:
                    render_not_rendered["hit"] = True
                    log("⚠️ captchaNotRendered 上报: %s", u[:200])
            if req.method == "POST" and ("/assets/js/bundle" in u or "/api/v2/" in u or "/b/c/" in u):
                try:
                    pd = req.post_data or ""
                    from urllib.parse import parse_qs as _pqs
                    fields = {k: (v[0] if v else "") for k, v in _pqs(pd).items()}
                    if fields.get("tag"):
                        last_bundle_tag["tag"] = fields["tag"]
                    if fields.get("vid") and not session_vid["cookie"]:
                        session_vid["cookie"] = fields["vid"]
                    log("PX POST %s plen=%s vid=%s pc=%s tag=%s",
                        u.split(".net")[-1][:40], len(fields.get("payload") or ""),
                        (fields.get("vid") or "")[:24], (fields.get("pc") or "")[:12],
                        (fields.get("tag") or "")[:16])
                    plen = len(fields.get("payload") or "")
                    if plen >= 15000 or (fields.get("pc") and plen >= 4000):
                        rec = {
                            "path": u.split(".net")[-1][:48],
                            "plen": plen,
                            "vid": fields.get("vid") or "",
                            "pc": fields.get("pc") or "",
                            "tag": fields.get("tag") or "",
                        }
                        res.setdefault("press_posts", []).append(rec)
                        log("记录 payload/pc 同会话 POST vid=%s pc=%s plen=%s",
                            rec["vid"][:36], rec["pc"][:16], plen)
                except Exception:
                    pass

        def on_response(resp):
            u = resp.url
            if "/assets/js/bundle" in u or "/api/v2/collector" in u or "/b/c/beacon" in u or "/msft" in u:
                try:
                    body = resp.text()
                except Exception:
                    body = ""
                info = _parse_ob_solve(body, last_bundle_tag.get("tag") or "")
                if info.get("segs") or info.get("do") is not None:
                    bundle_obs.append(info)
                    log("collector/bundle status=%s segs=%s solve=%s px3=%s score=%s",
                        resp.status, info.get("segs"), info.get("solve_result"),
                        bool(info.get("has_px3")), info.get("score"))
                    if info.get("solve_result") is not None:
                        res["solve_result"] = info["solve_result"]
                    if info.get("has_px3"):
                        res["ob_has_px3"] = True

        page.on("request", on_request)
        page.on("response", on_response)

        # CDP 会话（cdp 后端用；firefox/webkit 可能没有）
        cdp = None
        try:
            cdp = ctx.new_cdp_session(page)
        except Exception as e:  # noqa: BLE001
            log("new_cdp_session 失败: %s", e)
            try:
                cdp = browser.new_browser_cdp_session()
            except Exception as e2:  # noqa: BLE001
                log("browser CDP 也不可用（firefox/webkit 正常）: %s", e2)

        # WebGL renderer 自检（确认软件渲染真的在跑）
        try:
            page.goto("about:blank", wait_until="domcontentloaded", timeout=15000)
            renderer = page.evaluate(
                """() => { try { const c=document.createElement('canvas');
                const gl=c.getContext('webgl'); if(!gl) return 'NO_WEBGL';
                const d=gl.getExtension('WEBGL_debug_renderer_info');
                return d?gl.getParameter(d.UNMASKED_RENDERER_WEBGL):gl.getParameter(gl.RENDERER);
                } catch(e){ return 'ERR:'+e; } }"""
            )
            log("WebGL renderer = %s", renderer)
        except Exception as e:  # noqa: BLE001
            log("WebGL 自检异常: %s", e)

        def _press_poll(pre_px3: str) -> bool:
            """轮询【可见】#px-captcha → CDP 真按 → 收割 press _px3。返回 saw_visible。"""
            t0 = time.time()
            press_rounds = 0
            max_press_rounds = int(os.environ.get("PX_PRESS_ROUNDS", "3"))
            saw_visible = False
            while time.time() - t0 < timeout_s:
                page.wait_for_timeout(1500)
                box = _visible_captcha_box(page)
                if box:
                    saw_visible = True
                    cx = box["x"] + box["width"] / 2.0
                    cy = box["y"] + box["height"] / 2.0
                    log("发现【可见】#px-captcha box=%s 中心=(%.2f,%.2f) rendered=%s",
                        {k: round(v, 1) for k, v in box.items()}, cx, cy, not render_not_rendered["hit"])
                    cur0 = _collect_px(ctx)
                    if cur0.get("pxvid") and not session_vid["cookie"]:
                        session_vid["cookie"] = cur0["pxvid"]
                    log("同会话锚点 captcha.js.v=%s cookie._pxvid=%s",
                        session_vid["captcha_js"][:36] or "?", session_vid["cookie"][:36] or "?")
                    if want_press and press_rounds < max_press_rounds:
                        n_obs_before = len(bundle_obs)
                        hold = random.randint(HOLD_MS_MIN, HOLD_MS_MAX)
                        backend = PRESS_BACKEND
                        if not backend:
                            try:
                                from os_press import xdotool_available as _xd_ok
                                if HEADFUL and _xd_ok()[0]:
                                    backend = "xdotool"
                                else:
                                    backend = "os_hid" if (OS_PRESS and HEADFUL) else "cdp"
                            except Exception:
                                backend = "os_hid" if (OS_PRESS and HEADFUL) else "cdp"
                        used = False
                        if backend == "xdotool":
                            try:
                                if _HERE not in sys.path:
                                    sys.path.insert(0, _HERE)
                                from os_press import xdotool_available, xdotool_press_hold, viewport_to_screen
                                ok, why = xdotool_available()
                                if ok:
                                    sx, sy = viewport_to_screen(page, cx, cy)
                                    log("开始 xdotool 真按 hold=%dms screen=(%.1f,%.1f)", hold, sx, sy)
                                    xdotool_press_hold(sx, sy, hold, lambda m: log("%s", m))
                                    used = True
                                    res["press_backend"] = "xdotool"
                                else:
                                    log("xdotool 不可用（%s），回退 pw_locator", why)
                                    backend = "pw_locator"
                            except Exception as e:  # noqa: BLE001
                                log("xdotool 真按异常，回退 pw_locator: %s", e)
                                backend = "pw_locator"
                        if backend == "os_hid":
                            try:
                                if _HERE not in sys.path:
                                    sys.path.insert(0, _HERE)
                                from os_press import os_press_available, os_press_hold, viewport_to_screen
                                ok, why = os_press_available()
                                if ok:
                                    sx, sy = viewport_to_screen(page, cx, cy)
                                    log("开始 OS-HID 真按 hold=%dms screen=(%.1f,%.1f)", hold, sx, sy)
                                    os_press_hold(sx, sy, hold, lambda m: log("%s", m))
                                    used = True
                                    res["press_backend"] = "os_hid"
                                else:
                                    log("OS-HID 不可用（%s），回退 pw_locator", why)
                                    backend = "pw_locator"
                            except Exception as e:  # noqa: BLE001
                                log("OS-HID 真按异常，回退 pw_locator: %s", e)
                                backend = "pw_locator"
                        if not used and backend in ("pw_locator", "locator"):
                            log("开始 Playwright locator.click delay 长按 hold=%dms round=%s", hold, press_rounds + 1)
                            try:
                                _pw_locator_press_hold(page, hold, log)
                                used = True
                                res["press_backend"] = "pw_locator"
                            except Exception as e:  # noqa: BLE001
                                log("locator 真按异常，回退 pw_mouse: %s", e)
                                backend = "pw_mouse"
                        if not used and backend in ("pw_iframe", "iframe"):
                            log("开始 Playwright iframe hover+mouse.down 长按 hold=%dms round=%s",
                                hold, press_rounds + 1)
                            try:
                                _pw_iframe_press_hold(page, hold, log)
                                used = True
                                res["press_backend"] = "pw_iframe"
                            except Exception as e:  # noqa: BLE001
                                log("pw_iframe 真按异常，回退 pw_mouse: %s", e)
                                backend = "pw_mouse"
                        if not used and backend == "pw_mouse":
                            log("开始 Playwright page.mouse 贝塞尔按住 hold=%dms slow=%s round=%s",
                                hold, HUMAN_SLOW, press_rounds + 1)
                            try:
                                _pw_mouse_press_hold(page, cx, cy, hold, log)
                                used = True
                                res["press_backend"] = "pw_mouse"
                            except Exception as e:  # noqa: BLE001
                                log("pw.mouse 真按异常，回退 CDP: %s", e)
                                backend = "cdp"
                        if not used:
                            if cdp is None:
                                log("无 CDP，无法回退 Input.dispatchMouseEvent")
                            else:
                                log("开始 CDP 真按 hold=%dms round=%s", hold, press_rounds + 1)
                                try:
                                    _cdp_press_hold(cdp, cx, cy, hold, log)
                                    res["press_backend"] = "cdp"
                                except Exception as e:  # noqa: BLE001
                                    log("CDP 真按异常: %s", e)
                        res["pressed"] = True
                        press_rounds += 1
                        page.wait_for_timeout(4000)
                        after = _collect_px(ctx)
                        a_px3 = after.get("px3", "")
                        cleared = _visible_captcha_box(page) is None
                        try:
                            st = _page_state(page)
                            heading = " ".join(st.get("heading", []))
                        except Exception:
                            heading = ""
                        new_obs = bundle_obs[n_obs_before:]
                        log("真按后：widget清除=%s heading=%s new_bundle=%s solve=%s",
                            cleared, heading[:60], len(new_obs),
                            (new_obs[-1].get("solve_result") if new_obs else None))
                        if a_px3 and a_px3 != pre_px3:
                            res.update(after)
                            res["press_px3_changed"] = True
                            res["challenge_cleared"] = cleared
                            log("✅ 真按后 _px3 变化: %s... 含:1000:=%s 清除=%s",
                                a_px3[:48], ":1000:" in a_px3, cleared)
                            if cleared or (new_obs and new_obs[-1].get("solve_result") == "0"):
                                break
                        else:
                            log("真按后 _px3 未变化: %s...", a_px3[:40])
                        if press_rounds >= max_press_rounds:
                            log("已完成 %d 轮真按，退出 poll（避免超时空转）", press_rounds)
                            break
                        continue
                    if want_press and press_rounds >= max_press_rounds:
                        log("真按轮次已满，退出 poll")
                        break
                # 静默模式：拿到 silent token 即可返回
                cur = _collect_px(ctx)
                if cur.get("px3"):
                    res.update(cur)
                if not want_press and cur.get("px3"):
                    log("silent 模式已拿到 _px3，返回")
                    break
                # want_press 且从未见可见挑战：继续等到超时（挑战可能在最后一步出现）
            return saw_visible

        # preseed 注册会话 PX cookie（若提供），使浏览器 press 绑定注册 vid；否则清空强制新签发
        if preseed_cookies:
            try:
                ctx.add_cookies(list(preseed_cookies))
                log("已预置注册会话 PX cookie(%d): %s", len(preseed_cookies),
                    ",".join(sorted({str(c.get("name", "")) for c in preseed_cookies})))
            except Exception as e:  # noqa: BLE001
                log("预置注册 cookie 失败: %s", e)
        else:
            try:
                ctx.clear_cookies()
                log("未预置 cookie：清空后现场签发")
            except Exception:
                pass

        # 定向模式策略：
        # parent=注册页注入 challenge iframe（推荐，与 HTTP 会话同 vid）
        # drive=signup 驱动流（会另起表单会话，仅作回退）
        # iframe=直连 challengeUrl（对照，通常无法渲染 press）
        strategy = (os.environ.get("PX_SWIFTSHADER_TARGET", "parent").strip().lower()
                    if targeted else "signup")

        if targeted and strategy == "iframe":
            # ── 对照：直连注册会话 challengeUrl（+ch_ctx=1） ──
            nav_url = challenge_url
            if "ch_ctx=" not in nav_url:
                nav_url += ("&" if "?" in nav_url else "?") + "ch_ctx=1"
            log("[iframe策略] 导航 URL（含 ch_ctx）=%s", nav_url)
            try:
                page.goto(nav_url, wait_until="domcontentloaded", timeout=45000)
                log("已直连注册 challengeUrl")
            except Exception as e:  # noqa: BLE001
                log("goto challengeUrl 异常: %s", e)
            page.wait_for_timeout(3500)

            pre = _collect_px(ctx)
            pre_px3 = pre.get("px3", "")
            res["px3_silent"] = pre_px3
            res.update({k: v for k, v in pre.items() if v})
            if pre.get("pxvid"):
                log("直连后 _pxvid=%s 注册vid=%s 匹配=%s",
                    pre.get("pxvid"), vid or "?", pre.get("pxvid") == vid)

            saw_visible = _press_poll(pre_px3)
            if want_press and not saw_visible:
                log("⚠️ [iframe策略] 直连 challengeUrl 未渲染 press（iframe 需父页上下文）")
        elif targeted and strategy == "parent":
            parent_url = (
                signup_page_url
                or os.environ.get("PX_SIGNUP_PAGE_URL", "").strip()
                or SIGNUP_URL
            )
            ch_url = challenge_url or ""
            extras: list[str] = []
            if vid and "v=" not in ch_url and "vid=" not in ch_url:
                extras.append(f"v={vid}")
            if uuid and "u=" not in ch_url and "uuid=" not in ch_url:
                extras.append(f"u={uuid}")
            if ch_url and "ch_ctx=" not in ch_url:
                extras.append("ch_ctx=1")
            if extras:
                ch_url += ("&" if "?" in ch_url else "?") + "&".join(extras)
            log("[parent策略] 注册页=%s", parent_url[:100])
            log("[parent策略] challenge=%s", ch_url[:120] if ch_url else "?")
            try:
                page.goto(parent_url, wait_until="domcontentloaded", timeout=45000)
                log("已打开注册页（HTTP 同 URL）")
            except Exception as e:  # noqa: BLE001
                log("goto 注册页异常: %s", e)
            page.wait_for_timeout(2000)
            aid = app_id or "PXzC5j78di"
            if uuid and vid:
                captcha_js = (
                    f"https://captcha.hsprotect.net/{aid}/captcha.js"
                    f"?a=c&m=0&u={uuid}&v={vid}"
                )
                try:
                    page.add_script_tag(url=captcha_js)
                    log("已加载 captcha.js（绑定 challenge vid/uuid）")
                except Exception as e:  # noqa: BLE001
                    log("add_script_tag captcha.js 失败: %s", e)
                page.wait_for_timeout(1500)
            if vid or uuid:
                try:
                    sc_result = page.evaluate(
                        """(data) => {
                          const payload = {
                            vid: data.vid || '',
                            uuid: data.uuid || '',
                            appId: data.appId || 'PXzC5j78di',
                            sessionId: data.sessionId || '',
                          };
                          const mount = document.getElementById('px-captcha-mount');
                          if (!mount) {
                            const d = document.createElement('div');
                            d.id = 'px-captcha-mount';
                            d.style.cssText = 'width:480px;margin:48px auto';
                            (document.body || document.documentElement).appendChild(d);
                          }
                          const trySet = (w) => {
                            if (!w) return false;
                            const g = w.PXPXzC5j78di || w['PXPXzC5j78di'];
                            if (g && typeof g.setChallenge === 'function') {
                              g.setChallenge(payload);
                              return true;
                            }
                            return false;
                          };
                          if (trySet(window)) return 'parent';
                          return 'skip';
                        }""",
                        {
                            "vid": vid or "",
                            "uuid": uuid or "",
                            "appId": aid,
                            "sessionId": session_id or "",
                        },
                    )
                    log("setChallenge 结果=%s", sc_result)
                except Exception as e:  # noqa: BLE001
                    log("setChallenge 异常: %s", e)
            if ch_url:
                try:
                    page.evaluate(
                        """(url) => {
                          let f = document.getElementById('px-challenge-frame');
                          if (!f) {
                            f = document.createElement('iframe');
                            f.id = 'px-challenge-frame';
                            f.title = 'Human verification';
                            f.style.cssText =
                              'width:480px;height:140px;border:none;display:block;margin:24px auto';
                            (document.body || document.documentElement).appendChild(f);
                          }
                          f.src = url;
                        }""",
                        ch_url,
                    )
                    log("已注入 challenge iframe（含 vid/uuid 参数）")
                except Exception as e:  # noqa: BLE001
                    log("注入 iframe 失败: %s", e)
            page.wait_for_timeout(4000)

            pre = _collect_px(ctx)
            pre_px3 = pre.get("px3", "")
            res["px3_silent"] = pre_px3
            res.update({k: v for k, v in pre.items() if v})
            if pre.get("pxvid"):
                log("parent 后 _pxvid=%s 注册vid=%s 匹配=%s",
                    pre.get("pxvid"), vid or "?", pre.get("pxvid") == vid)

            saw_visible = _press_poll(pre_px3)
            if want_press and not saw_visible:
                log("⚠️ [parent策略] 未渲染可见 press，尝试回退 drive 策略")
                try:
                    page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(2000)

                    def _check_stop_parent():
                        try:
                            return bool(_visible_captcha_box(page))
                        except Exception:
                            return False

                    _drive_signup(page, log, _check_stop_parent)
                    pre2 = _collect_px(ctx)
                    pre_px3 = pre2.get("px3", "") or pre_px3
                    res.update({k: v for k, v in pre2.items() if v})
                    saw_visible = _press_poll(pre_px3)
                except Exception as e:  # noqa: BLE001
                    log("parent→drive 回退失败: %s", e)
        else:
            # ── signup 驱动流（standalone 或 targeted+drive）：真按可渲染 ──
            if targeted:
                log("[drive策略] 已预置注册 _pxvid=%s，走 signup 驱动流令 press 绑定注册 vid", vid or "?")
            try:
                page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=45000)
                log("已打开 %s", SIGNUP_URL)
            except Exception as e:  # noqa: BLE001
                log("goto 异常: %s", e)

            page.wait_for_timeout(3500)

            def _check_stop():
                try:
                    if _visible_captcha_box(page):
                        log("驱动中检测到【可见】#px-captcha 挑战，停止推进表单，转入真按")
                        return True
                except Exception:
                    pass
                return False

            _drive_signup(page, log, _check_stop)

            # 记录“驱动完成后（真按前）”的 silent token，用于区分 press token 是否真的由按住产生
            pre = _collect_px(ctx)
            pre_px3 = pre.get("px3", "")
            res["px3_silent"] = pre_px3
            if pre_px3:
                log("驱动后已存在【silent】_px3（未按住）: %s... 含:1000:=%s",
                    pre_px3[:44], ":1000:" in pre_px3)
            res.update({k: v for k, v in pre.items() if v})

            saw_visible = _press_poll(pre_px3)
            if want_press and not saw_visible:
                log("⚠️ 本次运行全程未出现【可见】#px-captcha 挑战（PX 在该 IP/会话静默放行）；"
                    "无 press 可收割，返回的是 silent token（pressed=False）")

        # 收尾：截图 + cookie 快照
        try:
            shot = os.path.join(_HERE, f"px_swiftshader_shot_{run_id}.png")
            page.screenshot(path=shot, full_page=False)
            log("截图 -> %s", shot)
        except Exception as e:  # noqa: BLE001
            log("截图失败: %s", e)

        final = _collect_px(ctx)
        res.update({k: v for k, v in final.items() if v})
        log("cookie 快照 px3=%s pxvid=%s pxde=%s",
            (final.get("px3") or "")[:40], (final.get("pxvid") or "")[:24], (final.get("pxde") or "")[:24])
        log("网络事件(%d): %s", len(net_events), " | ".join(net_events[-8:]) if net_events else "无")
        log("captchaNotRendered 命中 = %s", render_not_rendered["hit"])
        harvested_vid = final.get("pxvid") or ""
        anchor = session_vid["captcha_js"] or session_vid["cookie"] or (vid or "")
        res["session_vid"] = anchor
        res["captcha_js_vid"] = session_vid["captcha_js"]
        res["captcha_js_uuid"] = session_vid["uuid"]
        if targeted:
            res["reg_vid"] = vid or ""
            res["target_session_id"] = session_id or ""
            res["vid_match_reg"] = bool(vid) and harvested_vid == vid
        lpv = (res.get("last_press_vid") or "").strip()
        if targeted and vid and lpv:
            res["press_vid_match_reg"] = lpv == vid
        else:
            res["press_vid_match_reg"] = res.get("vid_match_reg", False)
        res["vid_match"] = bool(anchor) and harvested_vid == anchor
        posts = res.get("press_posts") or []
        if posts:
            last = posts[-1]
            res["last_pc"] = last.get("pc", "")
            res["last_press_vid"] = last.get("vid", "")
            res["pc_vid_match"] = bool(last.get("vid")) and last["vid"] == harvested_vid
            log("【payload/pc】posts=%d last_pc=%s last_vid=%s pc_vid_match=%s",
                len(posts), (last.get("pc") or "")[:16], (last.get("vid") or "")[:36],
                res["pc_vid_match"])
        log("【同会话 vid】收割=%s 锚点=%s(js=%s cookie=%s) match=%s solve=%s",
            harvested_vid or "?", anchor or "?", session_vid["captcha_js"][:24],
            session_vid["cookie"][:24], res["vid_match"], res.get("solve_result"))
        if bundle_obs:
            res["bundle_obs"] = bundle_obs[-8:]

        try:
            browser.close()
        except Exception:
            pass

    log("harvest 结束 pressed=%s px3?=%s has_1000=%s vid_match=%s press_vid_match=%s",
        res["pressed"], bool(res["px3"]),
        (":1000:" in res["px3"]) if res["px3"] else False,
        res.get("vid_match", "N/A"), res.get("press_vid_match_reg", "N/A"))
    return res


if __name__ == "__main__":
    import sys
    proxy_arg = None
    if len(sys.argv) > 1 and sys.argv[1] not in ("-", "direct", "DIRECT"):
        proxy_arg = sys.argv[1]
    elif os.environ.get("PX_PROXY"):
        proxy_arg = os.environ["PX_PROXY"]
    out = harvest(proxy_arg, want_press=True, timeout_s=int(os.environ.get("PX_HARVEST_TIMEOUT", "90")))
    print(json.dumps({k: v for k, v in out.items() if k != "bundle_obs"} | {
        "has_1000": (":1000:" in (out.get("px3") or "")),
        "px3_prefix": (out.get("px3") or "")[:48],
        "last_obs": out.get("bundle_obs"),
    }, ensure_ascii=False, indent=2))
