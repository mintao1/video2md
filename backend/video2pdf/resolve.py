"""URL 归一化与条目展开（B站/YouTube/抖音/小红书；国内站子进程剥代理直连）。"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .bilibili import accepts_url as _bilibili_public_url
from .bilibili import resolve_public_video
from .util import redact

# backwards-compatible single-entry wrapper


_BARE_HOST_RE = re.compile(r"^(?:www\.)?[\w-]+(?:\.[\w-]+)*\.(?:com|cn|net|org|tv|io|me|be|co|xyz|app|cc|to)"
                           r"(?:[/?#]|$)", re.I)


_SHARE_URL_RE = re.compile(r"https?://[^\s一-鿿\"'「」『』<>，。；]+")


def extract_url(text: str) -> str:
    """剥分享文本取 URL（「复制打开抖音【…】 https://v.douyin.com/xx 其余文案」→ 纯链接）。

    移植自 Dify code 节点 CODE_CLEAN_URL——Dify 抽离后这一步收回 resolve 链入口。
    抖音弹窗链接（modal_id）归一化在 normalize_douyin_url，此处只管提取。
    """
    t = (text or "").strip()
    m = _SHARE_URL_RE.search(t)
    return m.group(0) if m else t




def ensure_scheme(u: str) -> str:
    """补 URL scheme：从地址栏复制的 `www.bilibili.com/video/BV…` 没有 `https://`。

    不补的话整条链按「本地文件路径」走（str.startswith 检查处处为假），
    一直拖到 download 阶段才炸 `文件不存在: …/www.bilibili.com/video/BV…`。
    判定宽松但两道闸：已存在本地文件一律不动；只对「像域名开头」的串补 https。
    """
    u = (u or "").strip()
    if not u or u.startswith(("http://", "https://", "/", "~")):
        return u
    if Path(u).expanduser().exists():  # 本地真存在这个文件 → 按路径处理
        return u
    return f"https://{u}" if _BARE_HOST_RE.match(u) else u



def normalize_url(u: str) -> str:
    """链接归一化：剥掉 spm_id_from/vd_source 等跟踪参数，幂等去重才不被参数差异绕过。"""
    m = re.match(r"(https?://[^/]+/video/(BV\w+))/?\??([^#]*)", u)
    if m:  # B站：只保留 BV 号 + 分P 参数（?p=N 决定处理哪一集，不能丢）
        qs = dict(re.findall(r"(?:^|&)([^=&]+)=([^&]*)", m.group(3) or ""))
        return m.group(1) + "/" + (f"?p={qs['p']}" if qs.get("p") else "")
    m = re.match(r"(https?://(?:www\.)?youtube\.com/watch)\?([^#]+)", u)
    if m:
        qs = dict(re.findall(r"(?:^|&)([^=&]+)=([^&]*)", m.group(2)))
        if qs.get("list"):
            # 有 list= 时保留列表 ID（否则会把整张播放列表误判成单视频）
            return f"{m.group(1)}?list={qs['list']}" + (f"&v={qs['v']}" if qs.get("v") else "")
        if qs.get("v"):
            return f"{m.group(1)}?v={qs['v']}"
    m = re.match(r"(https?://youtu\.be/([\w-]+))(\?[^#]*\blist=([\w-]+))?", u)
    if m:
        # 短链本身是单视频；带 list= 时保留列表上下文（解析阶段据此展开整张列表）
        return m.group(1) + (f"?list={m.group(4)}" if m.group(4) else "")
    return u



def expand_short_link(u: str) -> str:
    """抖音/小红书短链先跟随重定向：yt-dlp 的这两个 extractor 只认 www 长链。"""
    if re.match(r"https?://(xhslink\.com|v\.douyin\.com)/", u):
        try:
            r = httpx.get(u, follow_redirects=True, timeout=20,
                          headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
            if r.url and str(r.url).startswith("http"):
                return str(r.url)
        except Exception:
            pass
    return u



_DOUYIN_MODAL_RE = re.compile(r"douyin\.com/[^\s?#]*\?(?:[^#\s]*&)?modal_id=(\d+)")


_DOUYIN_USER_RE = re.compile(r"douyin\.com/user/([\w-]+)")  # 博主主页链接（枚举全部作品）


# 短链 v.douyin.com 跟跳转后的落地形态之一：iesdouyin.com/share/user/<sec_uid>（博主主页分享）。
# 不识别这层，主页短链会漏到 yt-dlp 单视频链，「链接解析失败（已重试 3 次）」（2026-09-21 实测）
_DOUYIN_SHARE_USER_RE = re.compile(r"douyin\.com/share/user/([\w-]+)")


_DOUYIN_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")


_TTWID_CACHE = Path.home() / ".cache" / "video2pdf" / "douyin-ttwid.txt"



def normalize_douyin_url(u: str) -> str:
    """抖音链接归一化：博主主页（含 share/user 落地页）剥页态参数；页内弹窗链接 → /video/<id>；顺带剥空白。"""
    u = (u or "").strip()
    m = _DOUYIN_USER_RE.search(u) or _DOUYIN_SHARE_USER_RE.search(u)
    if m:
        # 主页链接常带 ?from_tab_name=main&vid=<某条视频id>（在主页点开过视频的地址栏态），
        # 用户意图是整个博主 → 只留 sec_uid，全部作品走枚举
        return f"https://www.douyin.com/user/{m.group(1)}"
    m = _DOUYIN_MODAL_RE.search(u)
    return f"https://www.douyin.com/video/{m.group(1)}" if m else u



def _douyin_ttwid() -> str:
    """抖音匿名设备 cookie（ttwid）：缓存 7 天，过期经字节注册端点重签，无需登录。"""
    try:
        if _TTWID_CACHE.exists() and time.time() - _TTWID_CACHE.stat().st_mtime < 7 * 86400:
            t = _TTWID_CACHE.read_text().strip()
            if t:
                return t
    except OSError:
        pass
    try:
        r = httpx.post("https://ttwid.bytedance.com/ttwid/union/register/",
                       json={"region": "cn", "aid": 1768, "needFid": False, "service": "www.ixigua.com",
                             "migrate_info": {"ticket": "", "source": "node"},
                             "cbUrlProtocol": "https", "union": True}, timeout=15)
        ttwid = r.cookies.get("ttwid") or ""
        if not ttwid:
            m = re.search(r"ttwid=([^;]+)", r.headers.get("set-cookie", ""))
            ttwid = m.group(1) if m else ""
    except Exception:
        return ""
    if ttwid:
        try:
            _TTWID_CACHE.parent.mkdir(parents=True, exist_ok=True)
            _TTWID_CACHE.write_text(ttwid)
        except OSError:
            pass
    return ttwid



def _find_aweme(o, depth: int = 0):
    """在 _ROUTER_DATA 里递归找含 play_addr.uri 的条目对象（不赌分享页的 key 命名）。"""
    if depth > 6:
        return None
    if isinstance(o, dict):
        va = o.get("video") or o.get("video_info") or {}
        if isinstance(va, dict) and ((va.get("play_addr") or {}).get("uri")):
            return o
        for v in o.values():
            f = _find_aweme(v, depth + 1)
            if f is not None:
                return f
    elif isinstance(o, list):
        for v in o:
            f = _find_aweme(v, depth + 1)
            if f is not None:
                return f
    return None



def douyin_direct_resolve(url: str) -> dict:
    """无登录 cookie 场景的抖音兜底：带 ttwid 抓移动分享页，抽条目元数据 + 无水印直链。"""
    m = re.search(r"/video/(\d+)", url) or re.search(r"modal_id=(\d+)", url)
    if not m:
        raise RuntimeError("抖音直链解析失败：未找到视频 id")
    aweme_id = m.group(1)
    try:
        r = httpx.get(f"https://www.iesdouyin.com/share/video/{aweme_id}", follow_redirects=True,
                      timeout=20, headers={"User-Agent": _DOUYIN_UA,
                                           "Cookie": f"ttwid={_douyin_ttwid()}"})
    except Exception as exc:
        raise RuntimeError(f"抖音分享页请求失败：{exc}") from exc
    rm = re.search(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>", r.text, re.S)
    if not rm:
        raise RuntimeError("抖音分享页无数据（可能触发风控；请稍后重试）")
    loader = json.loads(rm.group(1)).get("loaderData") or {}
    it = _find_aweme(loader)
    uri = ((it.get("video") or {}).get("play_addr") or {}).get("uri") if it else ""
    if not uri:
        raise RuntimeError("抖音分享页未取到视频地址（可能是图文笔记或风控）")
    author = (it.get("author") or {}) if it else {}
    return {"url": f"https://www.douyin.com/video/{aweme_id}", "id": aweme_id,
            "playlist_index": None, "playlist_title": "", "playlist_id": "",
            "uploader": (author.get("nickname") or author.get("unique_id") or "").strip(),
            "title": (it.get("desc") or "").strip(),
            "duration": int(it.get("duration") or 0) // 1000 or None,
            "direct_url": f"https://aweme.snssdk.com/aweme/v1/play/?video_id={uri}&ratio=720p&line=0"}



def _download_direct(url: str, dst: Path, *, headers: dict | None = None) -> Path:
    """绕过 yt-dlp 的直链下载（抖音 play 直链 302 到 douyinvod CDN；百度 dlink 同路）。"""
    hdrs = dict(headers or {"User-Agent": _DOUYIN_UA,
                            "Referer": "https://www.douyin.com/"})
    # 百度 dlink 单文件可上 GB、网盘节点慢，60s 不够；带宽×超时取 600s 上限
    with httpx.stream("GET", url, follow_redirects=True, timeout=600, headers=hdrs) as r:
        r.raise_for_status()
        with open(dst, "wb") as f:
            for chunk in r.iter_bytes(1 << 18):
                f.write(chunk)
    if dst.stat().st_size < 1024:
        raise RuntimeError("直链下载内容过小，疑似风控页")
    return dst



# ---------------------------------------------------------------- ③c 抖音博主主页枚举（CDP 驱动本机 Chrome）
# 为什么不用 yt-dlp/裸 API：抖音用户页纯客户端渲染（HTML 无 SSR 数据），
# post 列表接口被 Argus 签名（a_bogus）拦死；yt-dlp 也无 /user/ extractor。
# 让已登录抖音的 Chrome 自己完成签名：CDP 打开主页 → 滚动触发懒加载 →
# 拦截 aweme/v1/web/aweme/post/ 的 XHR 响应，一次拿到全部作品的完整元数据
# （视频直链 uri / 图文 images 列表都在响应里，下载阶段不用再抓详情页）。
_CDP_HTTP = "http://127.0.0.1:9222"



def _douyin_aweme_to_entry(a: dict, order: int) -> dict:
    """抖音 aweme JSON → 统一 entry（图文与视频分流在 media_type）。"""
    aid = str(a.get("aweme_id") or "")
    images = a.get("images") or []
    is_photo = bool(images)
    author = a.get("author") or {}
    video = a.get("video") or {}
    uri = ((video.get("play_addr") or {}).get("uri")) or ""
    entry = {"url": f"https://www.douyin.com/photo/{aid}" if is_photo else f"https://www.douyin.com/video/{aid}",
             "id": aid, "playlist_index": order,
             "title": (a.get("desc") or "").strip(),
             "uploader": (author.get("nickname") or "").strip(),
             "media_type": "image_post" if is_photo else "video"}
    if is_photo:
        entry["images"] = [u for img in images for u in (img.get("url_list") or [])[:1] if u]
    else:
        entry["duration"] = int(video.get("duration") or 0) // 1000 or None
        if uri:
            entry["direct_url"] = (f"https://aweme.snssdk.com/aweme/v1/play/?video_id={uri}"
                                   "&ratio=720p&line=0")
    return entry



def douyin_user_resolve(url: str, *, max_pages: int = 40, budget_s: float = 240.0) -> list[dict]:
    """枚举博主全部作品：CDP 打开主页滚动加载，拦截 post 接口响应。

    前置：Chrome 开着远程调试（--remote-debugging-port=9222）且已登录抖音。
    返回的 entries 带完整元数据（title/duration/direct_url/images），feed 顺序（新→旧）。
    """
    try:
        import websocket  # websocket-client；serve 进程依赖（pyproject 已声明）
    except ImportError as exc:
        raise RuntimeError("缺少 websocket-client 依赖（uv sync 后重试）") from exc

    m = _DOUYIN_USER_RE.search(url)
    if not m:
        raise RuntimeError(f"不是抖音博主主页链接：{url}")
    sec_uid = m.group(1)
    page_url = f"https://www.douyin.com/user/{sec_uid}"

    # 枚举结果磁盘缓存（6h）：同一博主反复跑/重试不再碰抖音页面 —— 今晚的软风控
    # 就是调试期重复扫主页堆出来的，缓存是风控的第一道防线
    cache_file = Path.home() / ".cache" / "video2pdf" / f"douyin-user-{sec_uid[:24]}.json"
    try:
        if cache_file.exists() and time.time() - cache_file.stat().st_mtime < 6 * 3600:
            cached = json.loads(cache_file.read_text())
            if isinstance(cached, list) and cached:
                print(f"♻️ 博主枚举走缓存（{len(cached)} 个作品，{int((time.time()-cache_file.stat().st_mtime)//60)} 分钟前）")
                return cached
    except Exception:
        pass

    try:
        httpx.get(f"{_CDP_HTTP}/json/version", timeout=5)
    except Exception as exc:
        raise RuntimeError("抖音博主枚举需要本机 Chrome 开着远程调试端口 9222（且已登录抖音）"
                             f"；当前连不上：{exc}") from exc
    # about:blank 开签、挂好监听再 navigate：直接带 URL 开签，首屏 post XHR 可能抢在
    # Network.enable 前跑完——作品少到不翻页的博主全程只有这一条 XHR，漏接即判失败
    # （2026-09-23 实测：11 作品博主稳定复现）；且重页面加载会拖垮 /json/new 的响应
    # （httpx 超时但页签其实已建，还会漏签）。
    try:
        tab = httpx.put(f"{_CDP_HTTP}/json/new?about:blank", timeout=10).json()
    except Exception:
        # 响应丢了就从页签表认领最新的 about:blank（建好没收到回复的情形）；没有才真失败
        blanks = [t for t in httpx.get(f"{_CDP_HTTP}/json", timeout=5).json()
                  if t.get("type") == "page" and t.get("url") == "about:blank"]
        if not blanks:
            raise
        tab = blanks[-1]
    tab_id, ws_url = tab["id"], tab["webSocketDebuggerUrl"]
    # Chrome 窗口被遮挡/最小化时渲染停摆 → IntersectionObserver 不触发 → 懒加载不翻页。
    # 扫描期间把窗口唤到前台（实测：可见时 27/27，遮挡时只能拿到第一页）
    with contextlib.suppress(Exception):
        subprocess.run(["open", "-a", "Google Chrome"], capture_output=True, timeout=5)
        httpx.get(f"{_CDP_HTTP}/json/activate/{tab_id}", timeout=5)

    awemes: dict[str, dict] = {}   # aweme_id → aweme（置顶会重复出现，按 id 去重）
    author_name = ""
    has_more, max_cursor = True, None
    mid = 0
    ws = None
    try:
        # suppress_origin：Chrome 111+ 拒绝带 Origin 头的 WS（除非 --remote-allow-origins），
        # 抑制 Origin 后握手放行
        ws = websocket.create_connection(ws_url, timeout=10, suppress_origin=True)

        def send(method: str, params: dict | None = None) -> int:
            nonlocal mid
            mid += 1
            ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            return mid

        send("Network.enable")
        send("Page.enable")
        send("Page.navigate", {"url": page_url})  # 监听就绪后才导航：首屏 XHR 必被捕获
        body_calls: dict[int, str] = {}  # 消息 id → requestId
        t0 = time.time()
        last_n, stale_scrolls = -1, 0
        pages = 0
        # 首条 post XHR 等待上限。2026-09-23 实测：风控预热（verify_center 质询）会把首 XHR
        # 拖到 16s+，质询重的时段超 25s 被误判「未登录」——放宽到 90s，真死会话代价只是多等一分钟
        first_xhr_deadline = t0 + 90
        expect_n: int | None = None   # 页面「作品 N」声明的总数（确定性的翻页停止条件）

        def read_expect() -> None:
            nonlocal expect_n
            i = send("Runtime.evaluate", {"expression":
                     "(document.body.innerText.match(/作品[\\s\\n]*([\\d.]+[万亿]?)/)||[])[1]||''",
                     "returnByValue": True})
            # 结果在后续消息循环里回收（body_calls 同款套路太绕，单独一个表）
            expect_calls[i] = True

        expect_calls: dict[int, bool] = {}
        while time.time() - t0 < budget_s and has_more and pages < max_pages:
            if expect_n is None and awemes:
                read_expect()  # 首批作品到了再读统计（DOM 已渲染）
            # 抖音用户页作品流在 .route-scroll-container（主滚动条不是 window）。
            # 按视口步进滚动（不是直跳到底）：懒加载哨兵元素需要真正进入可视区才触发，
            # 跳底会让 IntersectionObserver 错过；到顶后重新到底捕捉新增内容
            send("Runtime.evaluate", {"expression":
                 "var c=document.querySelector('.route-scroll-container')||document.scrollingElement;"
                 "var step=Math.max(400,(c.clientHeight||800)*0.8);"
                 "window.__scr=(window.__scr||0)+step;"
                 "if(c.scrollHeight && window.__scr>c.scrollHeight) window.__scr=c.scrollHeight;"
                 "if(c.scrollTop+c.clientHeight>=c.scrollHeight-5 && window.__scr>=c.scrollHeight){window.__scr=c.scrollHeight;}"
                 "c.scrollTop=window.__scr;"})
            deadline = time.time() + 2.5
            while time.time() < deadline:
                try:
                    ws.settimeout(max(0.2, deadline - time.time()))
                    msg = json.loads(ws.recv())
                except Exception:
                    break
                if "id" in msg:
                    if expect_calls.pop(msg["id"], False):
                        raw = ((msg.get("result") or {}).get("result") or {}).get("value") or ""
                        mnum = re.match(r"([\d.]+)(万|亿)?", str(raw))
                        if mnum:
                            n = float(mnum.group(1))
                            if mnum.group(2) == "万":
                                n *= 10000
                            elif mnum.group(2) == "亿":
                                n *= 1e8
                            expect_n = int(n)
                        continue
                    rid = body_calls.pop(msg["id"], None)
                    if rid is not None:
                        body = (msg.get("result") or {}).get("body") or ""
                        try:
                            data = json.loads(body)
                        except Exception:
                            continue
                        for a in data.get("aweme_list") or []:
                            aid = str(a.get("aweme_id") or "")
                            if aid and aid not in awemes:
                                awemes[aid] = a
                                author_name = author_name or ((a.get("author") or {}).get("nickname") or "").strip()
                        if not data.get("has_more"):
                            has_more = False
                        max_cursor = data.get("max_cursor", max_cursor)
                    continue
                if msg.get("method") == "Network.responseReceived":
                    resp = (msg.get("params") or {}).get("response") or {}
                    rurl = resp.get("url") or ""
                    if "aweme/v1/web/aweme/post" in rurl:
                        rid = (msg.get("params") or {}).get("requestId", "")
                        if rid:
                            pages += 1
                            body_calls[send("Network.getResponseBody", {"requestId": rid})] = rid
            if not awemes and time.time() > first_xhr_deadline:
                break  # 等够久还没有首条 XHR：登录态/风控问题，别再空滚
            if expect_n and len(awemes) >= expect_n:
                break  # 达到页面声明的作品总数，确定性收工
            if len(awemes) == last_n:
                stale_scrolls += 1
                # 已知总数却不够 → 更耐心滚（翻页没触发多半是滚动节奏/可见性问题）
                limit = 16 if (expect_n and len(awemes) < expect_n) else 8
                if stale_scrolls >= limit:
                    break
            else:
                last_n, stale_scrolls = len(awemes), 0
        if not awemes:
            raise RuntimeError("博主主页未拉到作品列表（Chrome 未登录抖音，或触发风控；"
                               "请在 Chrome 打开该主页确认能看到作品后重试）")
    finally:
        try:
            if ws:
                ws.close()
        finally:
            with contextlib.suppress(Exception):
                httpx.get(f"{_CDP_HTTP}/json/close/{tab_id}", timeout=5)

    entries = [_douyin_aweme_to_entry(a, i + 1) for i, a in enumerate(awemes.values())]
    for e in entries:
        e["playlist_title"] = author_name
        e["playlist_id"] = sec_uid
    if expect_n and len(entries) < expect_n:
        print(f"⚠ 枚举缺口：页面声明 {expect_n} 个作品，只扫到 {len(entries)} 个"
              f"（滚动未触发全部懒加载；建议重试）")
    elif entries:
        with contextlib.suppress(Exception):
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(entries, ensure_ascii=False))
    return entries


def _clean_ytdlp_err(stderr: str) -> str:
    """yt-dlp stderr 去污：剥掉 requests/urllib3 版本警告行，保住真正的 ERROR 行。"""
    lines = [ln for ln in (stderr or "").splitlines()
             if "DependencyWarning" not in ln and "warnings.warn" not in ln]
    return "\n".join(lines).strip()[-500:]



# 国内视频站直连：no_proxy 通配符（*.bilibili.com）yt-dlp/urllib 根本不认（实测 serve 经
# 17898 代理出口被 B 站 412 风控打满，直连/7897 出口均正常）——子进程显式剥代理，不靠 no_proxy。
# 云端例外（2026-09-22）：机房 IP 被国内站风控（B站 412 秒拒），设 V2PDF_PROXY（如
# http://127.0.0.1:17897 隧道出口）后国内站也走该代理；V2PDF_PROXY_DOMESTIC=1 显式开启。
# V2PDF_PROXY 只作用于 yt-dlp 子进程，不污染进程内 httpx（LLM 中转直连更快）。
_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


_DIRECT_HOST_RE = re.compile(
    r"(bilibili\.com|bilivideo\.com|b23\.tv|douyin\.com|snssdk\.com|iesdouyin\.com"
    r"|douyinvod\.com|douyinpic\.com|xiaohongshu\.com|xhscdn\.com)", re.I)


def _explicit_direct_host(url: str) -> bool:
    """Match the operator's exact/suffix direct-host allowlist."""
    host = (urlsplit(url or "").hostname or "").rstrip(".").casefold()
    if not host:
        return False
    for raw in (os.environ.get("V2PDF_DIRECT_HOSTS") or "").split(","):
        entry = raw.strip().rstrip(".").casefold()
        if not entry:
            continue
        if entry.startswith("."):
            suffix = entry[1:]
            if host == suffix or host.endswith(entry):
                return True
        elif host == entry:
            return True
    return False



def _subprocess_env(url: str) -> dict | None:
    """yt-dlp 子进程的代理环境。国内站默认剥代理直连；境外站默认继承父进程。
    配了 V2PDF_PROXY 时显式注入（容器/服务器场景父进程可能没代理环境）；
    V2PDF_PROXY_DOMESTIC=1 时国内站也走代理（机房 IP 被风控的云上场景）。"""
    if _explicit_direct_host(url):
        env = dict(os.environ)
        for k in _PROXY_KEYS:
            env.pop(k, None)
        return env
    domestic = bool(_DIRECT_HOST_RE.search(url or ""))
    domestic_forced_proxy = domestic and \
        (os.environ.get("V2PDF_PROXY_DOMESTIC") or "").lower() in ("1", "true", "on")
    if domestic and not domestic_forced_proxy:
        env = dict(os.environ)
        for k in _PROXY_KEYS:
            env.pop(k, None)
        return env
    px = os.environ.get("V2PDF_PROXY", "")
    if px:
        env = dict(os.environ)
        for k in _PROXY_KEYS:
            env[k] = px
        env.setdefault("NO_PROXY", "127.0.0.1,localhost")
        env.setdefault("no_proxy", env["NO_PROXY"])
        return env
    return None


def resolve_url(url: str) -> list[dict]:
    """仅枚举 URL，不下载；单视频也统一返回一个 entry。

    瞬断重试：B站风控（412）是分钟级窗口（实测并发批量时一波 4 连挂，几分钟后手动即成功），
    3s/8s 不够盖窗口；5 次尝试 3/10/30/60s 退避 ≈ 最长 ~2.5min，仍在工作流 /runs 240s 读超时内。"""
    url = normalize_douyin_url(expand_short_link(url))
    if _DOUYIN_USER_RE.search(url):
        return douyin_user_resolve(url)  # 博主主页：CDP 枚举全部作品（视频+图文）
    ytdlp = shutil.which("yt-dlp") or "/Library/Frameworks/Python.framework/Versions/3.14/bin/yt-dlp"
    err = ""
    backoff = [3, 10, 30]
    for attempt in range(4):  # 4×60s+43s 退避 ≈ 最坏 283s，仍在工作流 /runs 300s 读超时内
        try:
            p = subprocess.run([ytdlp, "--flat-playlist", "--dump-single-json", "--simulate",
                                "--no-warnings", url], capture_output=True, text=True, timeout=60,
                               env=_subprocess_env(url))
        except subprocess.TimeoutExpired:
            if _bilibili_public_url(url):
                return resolve_public_video(url)
            raise
        if p.returncode == 0:
            break
        err = _clean_ytdlp_err(p.stderr)
        # 抖音：yt-dlp 需要（登录级）cookie，无 cookie 环境走分享页直链兜底
        if "douyin.com" in url and ("Fresh cookies" in err or "Unsupported URL" in err):
            return [douyin_direct_resolve(url)]
        if "Fresh cookies" in err:
            raise RuntimeError("抖音需要浏览器新鲜 cookies（请用 Chrome cookies 重试）")
        if "No video formats" in err:
            raise RuntimeError("小红书页面无可解析视频（可能是图文笔记或触发反爬；请提供 Chrome cookies）")
        if "Unsupported URL" in err or "Video unavailable" in err:
            raise RuntimeError(f"链接解析失败：{redact(err)}")  # 永久性错误不重试
        if _bilibili_public_url(url):
            return resolve_public_video(url)
        if attempt < len(backoff):
            time.sleep(backoff[attempt])
    else:
        raise RuntimeError(f"链接解析失败（已重试 {len(backoff)} 次）：{redact(err)}")
    try:
        obj = json.loads(p.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("yt-dlp 元数据响应不是合法 JSON") from exc
    if obj.get("_type") == "playlist":
        entries: list[dict] = []
        for e in obj.get("entries") or []:
            if not e:
                continue
            entries.append({"url": e.get("webpage_url") or e.get("url"),
                            "id": e.get("id", ""),
                            # yt-dlp 自带的 playlist_index 权威；缺失时用有效条目序号兜底
                            "playlist_index": e.get("playlist_index") or len(entries) + 1,
                            "playlist_title": obj.get("title", ""),
                            "playlist_id": obj.get("id", ""),
                            "uploader": obj.get("uploader", "")})
        return entries
    return [{"url": obj.get("webpage_url") or url, "id": obj.get("id", ""),
             "playlist_index": obj.get("playlist_index"), "playlist_title": "",
             "playlist_id": "", "uploader": obj.get("uploader", "")}]



def _part_selected(index: int | None, spec: str) -> bool:
    if not spec:
        return True
    try:
        for token in spec.split(","):
            if "-" in token:
                a, b = (int(x) for x in token.split("-", 1))
                if a <= (index or 0) <= b:
                    return True
            elif int(token) == (index or 0):
                # 与 range 分支同口径（index or 0）：单视频条目 playlist_index=None——
                # 严格 == 会让 "0" 永假（rescue 单视频补跑实证踩坑），口径必须一致
                return True
    except ValueError:
        raise ValueError("--part-items 格式应为 1,3-5")
    return False
