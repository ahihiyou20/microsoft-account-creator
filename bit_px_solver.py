#!/usr/bin/env python3
"""比特浏览器 PX 令牌收割器（供 Python 注册流程桥接调用）。

思路：浏览器用**与注册同一条 kookeey IP** 打开 signup.live.com、驱动一步触发 PX，
收割 _px3/_pxvid/_pxde（silent；若弹按住则真实按住后再收割），返回给调用方，
调用方(risk.py)把它当作 captcha.run 的 solution 复用（同 IP，微软才认）。

对外：harvest(proxy) -> {"px3","pxvid","pxde","pressed"}
"""
from __future__ import annotations
import os, time, random, string, threading
from typing import Optional

import requests

BIT_API = os.environ.get("BIT_API", "http://127.0.0.1:54345").rstrip("/")
_lock = threading.Lock()
_profile_cache: dict[str, str] = {}

# 本地比特 API 必须绕过 HTTP_PROXY 环境代理（否则 requests 会拿 .env 的 {sid} 模板当代理）
_local = requests.Session()
_local.trust_env = False


def _bit(path: str, body: dict, timeout: int = 30) -> dict:
    r = _local.post(f"{BIT_API}{path}", json=body, timeout=timeout, proxies={"http": None, "https": None})
    r.raise_for_status()
    return r.json()


def _parse(proxy: str):
    """兼容 http://user:pass@host:port 与 host:port:user:pass 两种格式。"""
    proxy = (proxy or "").strip()
    if "://" in proxy:
        from urllib.parse import urlparse
        u = urlparse(proxy)
        return u.hostname or "", str(u.port or ""), u.username or "", u.password or ""
    a = proxy.split(":")
    return a[0], a[1], a[2], ":".join(a[3:])


def ensure_profile_for(proxy: str) -> str:  
    """为该 proxy 复用/创建 profile，并把代理更新为该 proxy；proxy 为空 = 直连。"""  
    proxy = (proxy or "").strip()  
    with _lock:  
        pid = os.environ.get("BIT_PROFILE_ID", "").strip() or _profile_cache.get("pid", "")  
        body = {  
            "name": "px-solver", "remark": "px",  
            "browserFingerPrint": {  
                "coreVersion": "124", "ostype": "PC", "os": "Win32", "version": "124",  
                "isLanguageBaseIp": True, "isDisplayLanguageBaseIp": True,  
                "webRTC": "0",  
            },  
        }  
        if proxy:  
            host, port, user, pwd = _parse(proxy)  
            body.update({  
                "proxyMethod": 2, "proxyType": "http",  
                "host": host, "port": port,  
                "proxyUserName": user, "proxyPassword": pwd,  
            })  
        else:  
            body.update({  
                "proxyMethod": 2, "proxyType": "noproxy",  
                "host": "", "port": "",  
                "proxyUserName": "", "proxyPassword": "",  
            })  
        if pid:  
            body["id"] = pid  
        j = _bit("/browser/update", body)  
        if not j.get("success"):  
            raise RuntimeError(f"profile 更新失败: {j.get('msg')}")  
        pid = (j.get("data") or {}).get("id") or pid  
        _profile_cache["pid"] = pid  
        return pid


def _open(pid: str) -> str:
    j = _bit("/browser/open", {"id": pid}, timeout=180)
    ws = (j.get("data") or {}).get("ws") or ""
    if not ws:
        raise RuntimeError(f"open 未返回 ws: {j}")
    return ws


def _close(pid: str):
    try: _bit("/browser/close", {"id": pid}, timeout=30)
    except Exception: pass


def clear_profile_data(pid: str, *, close: bool = True) -> None:
    """清除比特 profile 的 cookie + 缓存（注册失败/风控后重置用）。"""
    try:
        _bit("/browser/cookies/clear", {"browserId": pid}, timeout=30)
    except Exception:
        pass
    try:
        _bit("/cache/clear", {"ids": [pid]}, timeout=60)
    except Exception:
        pass
    if close:
        _close(pid)
        time.sleep(1.5)


def _press_hold(page, box, hold_ms=9000):
    cx = box["x"] + box["width"]/2; cy = box["y"] + box["height"]/2
    for i in range(1, 8):
        page.mouse.move(cx-40+i*5+random.uniform(-2,2), cy-20+i*3+random.uniform(-2,2), steps=3); time.sleep(random.uniform(.02,.06))
    page.mouse.move(cx, cy, steps=5); time.sleep(random.uniform(.1,.2))
    page.mouse.down(); t0=time.time()
    while (time.time()-t0)*1000 < hold_ms:
        page.mouse.move(cx+random.uniform(-1.5,1.5), cy+random.uniform(-1.5,1.5), steps=1); time.sleep(random.uniform(.08,.16))
    page.mouse.up()


def _find_captcha(page):
    for fr in page.frames:
        try:
            el = fr.query_selector("#px-captcha")
            if el:
                b = el.bounding_box()
                if b and b["width"] > 5: return fr, b
        except Exception: continue
    return None, None


def _collect_px(ctx) -> dict:
    out = {"px3": "", "pxvid": "", "pxde": ""}
    for c in ctx.cookies():
        if c["name"] == "_px3": out["px3"] = c["value"]
        elif c["name"] == "_pxvid": out["pxvid"] = c["value"]
        elif c["name"] == "_pxde": out["pxde"] = c["value"]
    return out


def harvest(proxy: str, *, want_press: bool = False, timeout_s: int = 40) -> dict:
    """打开浏览器，驱动 signup 触发 PX，收割 _px3/_pxvid/_pxde。"""
    from playwright.sync_api import sync_playwright
    pid = ensure_profile_for(proxy)
    ws = _open(pid)
    res = {"px3": "", "pxvid": "", "pxde": "", "pressed": False}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(ws)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            # 关键：清掉旧的 _px3/_pxvid，强制 PX 现场签发新鲜令牌（px3 60s 过期，缓存旧值会被微软拒→挑战）
            try:
                ctx.clear_cookies()
            except Exception:
                pass
            page.goto("https://signup.live.com/signup", wait_until="domcontentloaded", timeout=45000)
            time.sleep(5)
            # 驱动一步触发 _px3
            try:
                for sel in ('#usernameInput', 'input[name="MemberName"]', 'input[type="email"]', '#MemberName'):
                    el = page.query_selector(sel)
                    if el:
                        el.fill("".join(random.choice(string.ascii_lowercase) for _ in range(10))); break
                for sel in ('#nextButton', 'button[type="submit"]', 'input[type="submit"]', '#iSignupAction'):
                    b = page.query_selector(sel)
                    if b: b.click(); break
            except Exception:
                pass
            # 轮询等 _px3
            t0 = time.time()
            while time.time() - t0 < timeout_s:
                time.sleep(1.5)
                fr, box = _find_captcha(page)
                if box:
                    _press_hold(page, box, hold_ms=random.randint(8500, 10500)); res["pressed"] = True; time.sleep(4)
                cur = _collect_px(ctx)
                if cur["px3"]:
                    res.update(cur); break
                res.update(cur)  # 至少留 pxvid/pxde
            browser.close()
    finally:
        _close(pid)
    return res