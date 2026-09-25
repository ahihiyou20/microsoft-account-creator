#!/usr/bin/env python3
"""Path B：比特指纹浏览器驱动 signup.live.com 建号（浏览器天然过 PerimeterX）。

状态机逐页识别并填写：邮箱 → 密码 → 国家/生日 → 姓名 → PX(如弹则真实按住) → 建号。
建号成功后在同一比特窗口绑定恢复邮箱，再把 cookie 交给 Python 换 OAuth。

独立模块，不影响现有 captcha.run 纯协议链路。
"""
from __future__ import annotations
import os, re, time, random, string
from typing import Optional

from bit_px_solver import ensure_profile_for, _open, _close, clear_profile_data

# CDP 真按 + 可见 captcha 检测（懒加载，避免 import ss_register 拖慢启动）
from px_swiftshader_solver import _visible_captcha_box

PROXY = os.environ.get("PX_PROXY") or os.environ.get("HTTP_PROXY") or ""
FIRST_NAMES = ["James", "John", "Robert", "Michael", "David", "Mary", "Jennifer", "Linda", "Emma", "Olivia"]
LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Wilson", "Moore"]


def _rand_user():
    return "".join(random.choice(string.ascii_lowercase) for _ in range(3)) + \
           "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(7))


def _rand_pwd():
    return "Ab" + "".join(random.choice(string.ascii_letters + string.digits) for _ in range(11)) + "!7"


def _rotate_sticky_proxy(proxy: str) -> str:
    """每次建号换新 sticky ssid。同一条 IP 连着出号/失败后再出，容易隔夜 abuse。

    默认开；PX_ROTATE_IP=0 可钉死环境变量里的原 ssid。
    proofs / OAuth 必须继续用这一条，所以只在本轮开头换一次。
    """
    proxy = (proxy or "").strip()
    if not proxy or os.environ.get("PX_ROTATE_IP", "1").strip() == "0":
        return proxy
    if "{sid}" in proxy:
        return proxy.replace("{sid}", str(random.randint(10000000, 99999999)))
    if re.search(r"_ssid_\d+_", proxy):
        sid = str(random.randint(10000000, 99999999))
        return re.sub(r"_ssid_\d+_", f"_ssid_{sid}_", proxy, count=1)
    return proxy


try:
    import human_input_kit as hik
    _HUMAN = True
except Exception:
    _HUMAN = False

_LAST = [420.0, 320.0]  # 记录上一次鼠标位置，供贝塞尔连贯移动


def _hmove(page, x, y):
    """贝塞尔真人鼠标移动到 (x,y)，坐标带浮点抖动。"""
    x = float(x); y = float(y)
    try:
        if _HUMAN:
            path = hik.bezier_mouse_path(hik.Point(_LAST[0], _LAST[1]), hik.Point(x, y), steps=random.randint(18, 32))
            hik.move_mouse_along(page, path)
        else:
            page.mouse.move(x, y, steps=random.randint(12, 22))
    except Exception:
        try: page.mouse.move(x, y, steps=15)
        except Exception: pass
    _LAST[0], _LAST[1] = x, y


def _hmove_box(page, box):
    _hmove(page, box["x"] + box["width"] / 2 + random.uniform(-4, 4), box["y"] + box["height"] / 2 + random.uniform(-3, 3))


def _warmup(page):
    """热身：真实鼠标移动 + 滚动 + 空闲抖动，让 PX 从一开始就看到人类活动。"""
    try:
        for _ in range(random.randint(3, 5)):
            _hmove(page, random.uniform(150, 950), random.uniform(150, 620)); time.sleep(random.uniform(0.2, 0.6))
        if _HUMAN:
            try: hik.random_scroll(page, bursts=2)
            except Exception: pass
            try: hik.idle_jitter(page, moves=random.randint(3, 5))
            except Exception: pass
    except Exception:
        pass


def _press_hold(page, box):
    """真人化按住：贝塞尔靠近 → 按下 → 保持 1.5-2.8s（PX 校验 pressDuration 1-3s）→ 抬起。"""
    cx = box["x"] + box["width"] / 2; cy = box["y"] + box["height"] / 2
    _hmove(page, cx + random.uniform(-6, 6), cy + random.uniform(-4, 4)); time.sleep(random.uniform(0.15, 0.35))
    _hmove(page, cx, cy); time.sleep(random.uniform(0.1, 0.25))
    page.mouse.down()
    t0 = time.time(); dur = random.uniform(1.5, 2.8)
    while time.time() - t0 < dur:
        page.mouse.move(cx + random.uniform(-2.2, 2.2), cy + random.uniform(-1.6, 1.6), steps=random.randint(1, 3))
        time.sleep(random.uniform(0.05, 0.15))
    page.mouse.up()


def _find_captcha(page):
    box = _visible_captcha_box(page)
    return (page, box) if box else (None, None)


def _auto_px_press(page, res, log, attempt: int = 0) -> bool:
    """自动按住 PX：只用 OS HID 点 profile Chromium 窗。

    禁止先走 Playwright/CDP：合成事件 isTrusted=false，PX 会直接判 bot，后续真按也过不了。
    """
    if not _visible_captcha_box(page):
        return False
    log("  → OS-HID 自动按住 PX attempt=%d（不走 CDP）…", attempt)
    os.environ["PX_OS_PRESS"] = "1"
    try:
        cdp = page.context.new_cdp_session(page)
    except Exception as exc:
        log("  ✗ 无法创建 CDP 会话: %s", exc); return False
    from ss_register import _do_px_press
    outcome = _do_px_press(page, cdp, res, log, attempt=attempt)
    return outcome == "cleared"


def _click_next(page) -> bool:
    try:
        b = page.query_selector('button[type="submit"]')
        if b and b.bounding_box():
            _hmove_box(page, b.bounding_box()); time.sleep(random.uniform(0.15, 0.45))
    except Exception:
        pass
    for how in (lambda: page.get_by_role("button", name="Next").first.click(timeout=3500),
                lambda: page.get_by_role("button", name="Create account").first.click(timeout=3500),
                lambda: page.click('button[type="submit"]', timeout=3500)):
        try: how(); return True
        except Exception: continue
    return False


def _pick_dropdown(page, btn_sel, want_index=1):
    """Fluent v9 下拉：label 会拦截点击 → 用 force 点按钮/或点 label 打开 → 选 role=option。"""
    opened = False
    bid = btn_sel.lstrip("#")
    for how in (lambda: page.click(btn_sel, force=True, timeout=3000),
                lambda: page.click(f'label[for="{bid}"]', force=True, timeout=2000),
                lambda: page.focus(btn_sel) or page.keyboard.press("Enter")):
        try:
            how(); opened = True; break
        except Exception:
            continue
    if not opened:
        return False
    time.sleep(0.6)
    try:
        opts = page.get_by_role("option")
        n = opts.count()
        if n:
            opts.nth(min(want_index, n - 1)).click(); return True
    except Exception:
        pass
    return False


def _fill_input(page, el, text: str, *, clear: bool = True) -> bool:
    """点选并键入；SPA 重绘导致节点脱落时返回 False，由调用方重查。"""
    try:
        try:
            _hmove_box(page, el.bounding_box())
        except Exception:
            pass
        el.click()
        if clear:
            el.fill("")
        time.sleep(random.uniform(0.2, 0.5))
        page.keyboard.type(text, delay=random.randint(40, 110))
        return True
    except Exception:
        return False


def _alive(page) -> bool:
    try:
        return not page.is_closed()
    except Exception:
        return False


def register(proxy: str = PROXY, *, verbose: bool = True) -> dict:
    from playwright.sync_api import sync_playwright

    def log(msg, *a):
        if verbose:
            print(msg % a if a else msg, flush=True)

    proxy = _rotate_sticky_proxy(proxy or PROXY)
    if proxy:
        os.environ["PX_PROXY"] = proxy
        sid = re.search(r"_ssid_(\d+)_", proxy)
        log("▶ 本号 sticky ssid=%s（每次注册换新 IP；PX_ROTATE_IP=0 可关闭）",
            sid.group(1) if sid else "未识别")

    # 有固定 profile 就直接开（它已带 kookeey 代理），跳过易错的 update 校验
    fixed = os.environ.get("BIT_PROFILE_ID", "").strip()
    if fixed:
        if proxy:
            ensure_profile_for(proxy)  # 已有 profile 也同步代理
        pid = fixed
    else:
        pid = ensure_profile_for(proxy)
    email_user = _rand_user()
    email = email_user + "@outlook.com"
    pwd = _rand_pwd()
    res = {"email": email, "password": pwd, "created": False, "px_pressed": False, "final_url": "", "stage": "", "note": ""}

    # 默认 OS HID 真鼠标。CDP/Playwright 合成事件过不了 PX（人手能过就是这个原因）。
    if os.environ.get("PX_MANUAL", "0") == "0":
        try:
            from os_press import os_press_available
            ok, why = os_press_available()
            if ok:
                os.environ["PX_OS_PRESS"] = "1"
                log("▶ PX 自动按：OS 真鼠标（前置 BitBrowser profile 窗）")
            else:
                log("▶ OS-HID 不可用（%s），自动按会失败；改 PX_MANUAL=1 人手按", why)
        except Exception as exc:
            log("▶ OS-HID 探测失败: %s", exc)

    log("▶ 比特注册启动 profile=%s …", pid[:8])
    if os.environ.get("BIT_FRESH", "1") != "0":
        log("▶ 清除 cookie/缓存…")
        clear_profile_data(pid, close=True)
    log("▶ 打开比特窗口…")
    ws = _open(pid)
    log("▶ CDP ws: %s …", ws[:50])

    try:
        with sync_playwright() as pw:
            b = pw.chromium.connect_over_cdp(ws)
            ctx = b.contexts[0] if b.contexts else b.new_context()
            page = ctx.new_page()
            for p in list(ctx.pages):
                if p != page:
                    try: p.close()
                    except Exception: pass
            try: ctx.clear_cookies()
            except Exception: pass
            page.goto("https://signup.live.com/signup?lic=1&mkt=en-US", wait_until="domcontentloaded", timeout=90000)
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            time.sleep(random.uniform(3, 5))
            _warmup(page)  # 先制造真实鼠标活动，避免"零 mousemove=机器人"

            name_filled = False
            problem_hits = 0
            for step in range(22):
                if not _alive(page):
                    res["stage"] = "browser_closed"
                    res["note"] = "比特窗口被关闭，请保持窗口打开直至脚本结束"
                    log("  ✗ 浏览器页面已关闭，中止"); break
                time.sleep(2.5)
                url = page.url
                try: title = page.title()
                except Exception: title = ""
                try: low = (page.content() or "").lower()
                except Exception: low = ""
                log("[step %d] %s %s", step, title[:42], url[:66])
                if "account creation has been blocked" in low or "account creation has been blocked" in title.lower():
                    res["stage"] = "signup_blocked"
                    res["note"] = "微软 Account creation has been blocked，换 sticky IP"
                    log("  ✗ 建号被拦截 Account creation has been blocked")
                    break
                if "we ran into a problem" in low or "we ran into a problem" in title.lower():
                    problem_hits += 1
                    log("  ✗ 风控拒页 We ran into a problem (%d)", problem_hits)
                    if name_filled or problem_hits >= 2:
                        res["stage"] = "signup_risk_rejected"
                        res["note"] = "微软风控拒页 We ran into a problem，需换 sticky IP"
                        break
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=60000)
                    except Exception as exc:
                        log("  reload 失败: %s", exc)
                        res["stage"] = "signup_risk_rejected"
                        res["note"] = "风控拒页且刷新失败"
                        break
                    time.sleep(3)
                    continue
                problem_hits = 0
                if "signup.live.com" not in url and "account.live.com" not in url and "login.live.com" not in url:
                    if "google.com" in url or "accounts.google" in url:
                        res["note"] = "页面跳到 Chrome/Google 登录，非微软注册"
                        log("  ✗ 页面跑偏到 Google 登录，重新打开注册页…")
                        page.goto("https://signup.live.com/signup?lic=1&mkt=en-US", wait_until="domcontentloaded", timeout=60000)
                        time.sleep(3); continue

                # PX 按住：默认 CDP 自动按（PX_MANUAL=1 才等人手）
                _, box = _find_captcha(page)
                if box:
                    if os.environ.get("PX_MANUAL", "1") != "0":
                        log("  ⚠️ PX 手动模式：请在比特窗口按住按钮约 2 秒（不要按 8–10 秒），最多等 180s…")
                        tw = time.time()
                        while time.time() - tw < 180:
                            time.sleep(2)
                            if not _visible_captcha_box(page):
                                log("  ✓ 检测到挑战已通过，继续"); res["px_pressed"] = True; break
                        time.sleep(2); continue
                    px_try = int(res.get("_px_try", 0))
                    if _auto_px_press(page, res, log, attempt=px_try):
                        res["px_pressed"] = True
                        res["px_passed"] = True
                        log("  ✓ PX 自动按住已通过")
                    else:
                        res["_px_try"] = px_try + 1
                        if px_try >= 2:
                            res["stage"] = "px_failed"
                            res["note"] = "PX 自动按 3 次仍未过"
                            log("  ✗ PX 自动按 3 次仍未过，中止本轮换 IP")
                            break
                        time.sleep(2)
                    continue

                # 成功判定
                if any(k in url for k in ("account.microsoft.com", "outlook.live.com", "login.live.com/oauth20", "/fp/")):
                    res["created"] = True; res["stage"] = "created"; break
                if "you're all set" in low or "your account has been created" in low or "welcome to outlook" in low:
                    res["created"] = True; res["stage"] = "created"; break

                # proofs「加安全信息」→ 在比特窗口填恢复邮箱，再交接 cookie 去换 token
                if "proof" in url.lower() or "add security info" in low or "help us protect" in low or "let's protect" in low:
                    res["stage"] = "proofs"; res["note"] = "到达 proofs（加安全信息）"
                    res["created"] = True
                    res["proofs_url"] = url
                    try: res["proofs_html"] = page.content() or ""
                    except Exception: res["proofs_html"] = ""
                    log("  ✅ 账号已建成，到达 proofs/Add。dump 页面字段:")
                    try:
                        fields = page.eval_on_selector_all(
                            "input,select,button,[role=button]",
                            "els=>els.filter(e=>e.offsetParent).slice(0,25).map(e=>({t:e.tagName,type:e.type||'',id:e.id||'',name:e.name||'',ph:e.placeholder||'',txt:(e.innerText||e.value||'').slice(0,30)}))")
                        for f in fields: log("     %s", f)
                    except Exception as e:
                        log("  proofs dump 失败: %s", e)
                    try: page.screenshot(path=os.path.join(os.path.dirname(__file__), "proofs_page.png"))
                    except Exception: pass
                    from browser_proofs import bind_recovery_in_browser, browser_proofs_enabled
                    if browser_proofs_enabled():
                        bound = bind_recovery_in_browser(page, log)
                        res["recovery_email"] = bound.get("recovery_email", "")
                        res["recovery_password"] = bound.get("recovery_password", "")
                        res["proofs_method"] = bound.get("proofs_method", "browser_proofs")
                        if bound.get("ok"):
                            res["stage"] = "proofs_bound"
                            res["note"] = "浏览器已绑定恢复邮箱"
                            try: res["proofs_url"] = page.url
                            except Exception: pass
                        else:
                            res["stage"] = "proofs_failed"
                            res["note"] = bound.get("note") or "浏览器绑定恢复邮箱失败"
                            log("  ✗ 浏览器绑恢复邮箱失败: %s", res["note"])
                    try: res["_full_cookies"] = ctx.cookies()
                    except Exception: res["_full_cookies"] = []
                    break

                # 邮箱页
                el = page.query_selector('input[type="email"]')
                if el:
                    cur = ""
                    try:
                        cur = (el.input_value() or "").strip()
                    except Exception:
                        el = page.query_selector('input[type="email"]')
                        cur = ""
                    if el and not cur:
                        if not _fill_input(page, el, email):
                            el = page.query_selector('input[type="email"]')
                            if el and not _fill_input(page, el, email):
                                log("  填邮箱失败，下一轮重试")
                                continue
                        log("  填邮箱 %s", email)
                    time.sleep(random.uniform(0.6, 1.4)); _click_next(page); continue

                # 密码页
                el = page.query_selector('input[type="password"]')
                if el:
                    cur = ""
                    try:
                        cur = (el.input_value() or "").strip()
                    except Exception:
                        el = page.query_selector('input[type="password"]')
                        cur = ""
                    if el and not cur:
                        if not _fill_input(page, el, pwd, clear=False):
                            el = page.query_selector('input[type="password"]')
                            if el and not _fill_input(page, el, pwd, clear=False):
                                log("  填密码失败，下一轮重试")
                                continue
                        log("  填密码")
                    time.sleep(random.uniform(0.6, 1.4)); _click_next(page); continue

                # 生日/国家页（幂等重试直到前进）
                if page.query_selector('#BirthMonthDropdown') or page.query_selector('#BirthYear') or page.query_selector('[name="BirthYear"]'):
                    _pick_dropdown(page, '#BirthMonthDropdown', want_index=random.randint(1, 12)); time.sleep(0.4)
                    _pick_dropdown(page, '#BirthDayDropdown', want_index=random.randint(1, 27)); time.sleep(0.3)
                    y = page.query_selector('#BirthYear') or page.query_selector('[name="BirthYear"]') or page.query_selector('input[type="number"]')
                    if y and not (y.input_value() or "").strip():
                        y.click(); y.fill(str(random.randint(1990, 2000)))
                    log("  填生日"); _click_next(page); continue

                # 姓名页
                fn = page.query_selector('#firstNameInput') or page.query_selector('[name="FirstName"]')
                if fn:
                    ln = page.query_selector('#lastNameInput') or page.query_selector('[name="LastName"]')
                    if not (fn.input_value() or "").strip():
                        fn.click(); page.keyboard.type(random.choice(FIRST_NAMES), delay=30)
                    if ln and not (ln.input_value() or "").strip():
                        ln.click(); page.keyboard.type(random.choice(LAST_NAMES), delay=30)
                    log("  填姓名"); name_filled = True; _click_next(page); continue

                _click_next(page)

            if _alive(page):
                res["final_url"] = page.url
                res["cookies"] = [{"name": c["name"], "value": c["value"], "domain": c["domain"]} for c in ctx.cookies()]
                try: page.screenshot(path=os.path.join(os.path.dirname(__file__), "bit_reg_last.png"))
                except Exception: pass
            # 勿 browser.close()：会关掉比特窗口；只断开 CDP。窗口由下面 BIT_KEEP_OPEN 决定。
    except Exception as exc:
        res["stage"] = res.get("stage") or "browser_error"
        res["note"] = f"页面操作异常: {exc}"
        log("  ✗ %s", res["note"])
    finally:
        if os.environ.get("BIT_KEEP_OPEN", "1") != "0":
            if verbose:
                print("  比特窗口保持打开（BIT_KEEP_OPEN=1）")
        else:
            _close(pid)
            if verbose:
                print("  已关闭比特窗口（下次注册再开）")

    if os.environ.get("BIT_FINISH_PROOFS", "1") != "0" and res.get("stage") in (
        "proofs", "proofs_bound", "proofs_failed",
    ):
        full = res.pop("_full_cookies", None) or []
        if res.get("stage") == "proofs_failed":
            log("⚠️ 浏览器绑恢复邮箱失败，跳过 OAuth：%s", res.get("note", ""))
        elif full and (res.get("proofs_url") or res.get("final_url")):
            try:
                from ss_post import finish_after_proofs
                browser_bound = res.get("stage") == "proofs_bound" and bool(res.get("recovery_email"))
                if browser_bound:
                    log("── 浏览器已绑恢复邮箱，cookie 交接后只跑 OAuth ──")
                else:
                    log("── 续跑 proofs + OAuth（cookie 交接到 requests）──")
                post = finish_after_proofs(
                    email=email, password=pwd,
                    proofs_url=res.get("proofs_url", res.get("final_url", "")),
                    proofs_html=res.get("proofs_html", ""),
                    cookies=full, proxy=proxy or PROXY, country="US",
                    log=lambda m, *a: log(m % a if a else m),
                    proofs_done=browser_bound,
                    recovery_email=res.get("recovery_email", ""),
                    recovery_password=res.get("recovery_password", ""),
                    proofs_method=res.get("proofs_method", ""),
                )
                res["post"] = {k: v for k, v in post.items() if k != "refresh_token"}
                if post.get("status") == "ok":
                    res["stage"] = "complete"
                    res["recovery_email"] = post.get("recovery_email", "")
                    res["combo_path"] = post.get("combo_path", "")
                    res["snapshot"] = post.get("snapshot", "")
                    log("✅ 六段活号已导出 → %s", post.get("combo_path", "?"))
                else:
                    log("⚠️ proofs/OAuth 续跑未完成: %s", post.get("note", post.get("status")))
            except Exception as exc:
                log("⚠️ proofs/OAuth 续跑异常: %s", exc)
    res.pop("proofs_html", None)
    res.pop("_full_cookies", None)
    res.pop("recovery_password", None)
    return res


if __name__ == "__main__":
    import json
    r = register()
    r.pop("cookies", None)
    print("\n=== 结果 ===")
    print(json.dumps(r, ensure_ascii=False, indent=2))
