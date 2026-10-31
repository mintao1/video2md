"""resolve_url / 条目展开与幂等键的单测（不起网络、不起 Colab）。

跑法：python3 test_resolve.py
覆盖：单视频、B站分P（playlist JSON）、合集、短链归一、站点化错误提示、坏 JSON、--part-items 过滤。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/（包根）
from video2md import resolve as m

# m.subprocess / m.time 是全局模块：pytest 收集本文件时同进程的其它用例也会受影响，
# 跑完必须还原（见文末）。
_ORIG = (subprocess.run, m.time.sleep, m.douyin_user_resolve)
m.time.sleep = lambda _s: None  # resolve 重试退避在测试里不走真 sleep

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def fake_ytdlp(payload: dict | None = None, stderr: str = "", rc: int = 0, sink: list | None = None):
    """替换 subprocess.run，返回预制 yt-dlp 输出（sink 记录实际收到的 URL）。"""
    out = json.dumps(payload, ensure_ascii=False) if payload is not None else ""
    proc = subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=stderr)

    def run(*a, **kw):
        if sink is not None and a and a[0]:
            sink.append(a[0][-1])
        return proc
    return run


print("== 1. 单视频 → 一个条目 ==")
m.subprocess.run = fake_ytdlp({"id": "BV16mPLzgE62", "webpage_url": "https://www.bilibili.com/video/BV16mPLzgE62/",
                               "uploader": "Koala聊开源"})
e = m.resolve_url("https://www.bilibili.com/video/BV16mPLzgE62/")
check("单条", len(e) == 1, str(e))
check("带 uploader", e[0]["uploader"] == "Koala聊开源")
check("playlist_index 缺省为 None", e[0]["playlist_index"] is None)

print("== 2. B站多P / 合集（playlist JSON）==")
m.subprocess.run = fake_ytdlp({
    "_type": "playlist", "id": "2142762_3662502", "title": "合集·黑神话悟空", "uploader": "老戴在此",
    "entries": [{"id": "BV1a", "webpage_url": "https://www.bilibili.com/video/BV1a", "playlist_index": 1},
                {"id": "BV1b", "webpage_url": "https://www.bilibili.com/video/BV1b", "playlist_index": 2},
                None,
                {"id": "BV1c", "url": "https://www.bilibili.com/video/BV1c", "playlist_index": 3}]})
e = m.resolve_url("https://space.bilibili.com/2142762/lists/3662502?type=season")
check("3 条（跳过空条目）", len(e) == 3, str([x["id"] for x in e]))
check("继承合集标题", e[0]["playlist_title"] == "合集·黑神话悟空")
check("继承合集 id", e[0]["playlist_id"] == "2142762_3662502")
check("第 3 条索引回填 3", e[2]["playlist_index"] == 3, str(e[2]))
check("短 url 兜底", e[2]["url"].endswith("BV1c"))

print("== 3. --part-items 过滤 ==")
sel = [x for x in e if m._part_selected(x.get("playlist_index"), "1,3")]
check("取 1,3", [x["id"] for x in sel] == ["BV1a", "BV1c"], str([x["id"] for x in sel]))
sel = [x for x in e if m._part_selected(x.get("playlist_index"), "2-3")]
check("区间 2-3", [x["id"] for x in sel] == ["BV1b", "BV1c"])
check("空 spec 全选", all(m._part_selected(i, "") for i in (1, 2, 3)))

print("== 4. URL 归一化（幂等键 / 分P 不丢）==")
cases = [
    ("https://www.bilibili.com/video/BVabc123?p=2&spm_id_from=333.337", "https://www.bilibili.com/video/BVabc123/?p=2"),
    ("https://www.bilibili.com/video/BVabc123", "https://www.bilibili.com/video/BVabc123/"),
    ("https://www.youtube.com/watch?v=abc&list=PLxyz&index=3", "https://www.youtube.com/watch?list=PLxyz&v=abc"),
    ("https://www.youtube.com/watch?v=abc&utm_source=x", "https://www.youtube.com/watch?v=abc"),
    ("https://youtu.be/abc?list=PLxyz", "https://youtu.be/abc?list=PLxyz"),
]
for src, want in cases:
    got = m.normalize_url(src)
    check(f"{src[:52]}…", got == want, f"got {got}")

print("== 5. 站点化错误提示 ==")
for err, kw in [("ERROR: [Douyin] 123: Fresh cookies (not necessarily logged in) are needed", "抖音"),
                ("ERROR: [XiaoHongShu] abc: No video formats found!", "小红书"),
                ("ERROR: [BiliBili] 限流", "链接解析失败")]:
    m.subprocess.run = fake_ytdlp(None, stderr=err, rc=1)
    try:
        m.resolve_url("https://example.com/v")
        check(f"应抛错({kw})", False)
    except RuntimeError as exc:
        check(f"提示含「{kw}」", kw in str(exc), str(exc))

print("== 6. 坏 JSON ==")
class Bad:
    returncode = 0
    stdout = "not json"
    stderr = ""


m.subprocess.run = lambda *a, **kw: Bad()
try:
    m.resolve_url("https://example.com/v")
    check("应抛错", False)
except RuntimeError as exc:
    check("报 JSON 异常", "JSON" in str(exc), str(exc))

print("== 7. 短链重定向（xhslink / v.douyin）==")


class FakeResp:
    def __init__(self, url):
        self.url = url


seen = {}
probe = []


def fake_get(u, **kw):
    seen["url"] = u
    return FakeResp("https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9?xsec_token=AB")


m.httpx.get = fake_get
m.subprocess.run = fake_ytdlp({"id": "6411cf99", "webpage_url": "https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9",
                               "uploader": "某博主"}, sink=probe)
e = m.resolve_url("https://xhslink.com/a/abcdef")
check("短链被跟随", "xiaohongshu.com" in (probe[-1] if probe else ""), str(probe))
check("解析回单条目", len(e) == 1 and e[0]["id"] == "6411cf99")
check("长链不被改写", m.expand_short_link("https://www.bilibili.com/video/BV1a") == "https://www.bilibili.com/video/BV1a")

print("== 8. 抖音博主主页归一化（剥页态参数）==")
SEC = "MS4wLjABAAAAEdoinwc8bC1HTF95XlLojlOl-8FbK0jMnf7zsUyRDQIOhk6xSQilNHfupC3YWiNR"
u1 = f"https://www.douyin.com/user/{SEC}?from_tab_name=main&vid=7686046628160052393"
check("user 链接剥参数", m.normalize_douyin_url(u1) == f"https://www.douyin.com/user/{SEC}",
      m.normalize_douyin_url(u1))
check("user 路径下的 modal 仍按博主意图归一",
      m.normalize_douyin_url("https://www.douyin.com/user/x?modal_id=7686046628160052393")
      == "https://www.douyin.com/user/x",  # 主页优先：modal 在 user 路径下仍视为博主意图
      m.normalize_douyin_url("https://www.douyin.com/user/x?modal_id=7686046628160052393"))
check("非 user 的 modal 不受影响",
      m.normalize_douyin_url("https://www.douyin.com/discover?modal_id=123")
      == "https://www.douyin.com/video/123")

print("== 9. aweme → entry 映射（视频/图文分流）==")
video_aweme = {"aweme_id": "7686046628160052393", "desc": "口播视频",
               "author": {"nickname": "测试博主"},
               "video": {"duration": 618000, "play_addr": {"uri": "v0d00fb10000d2abc"}}}
photo_aweme = {"aweme_id": "7500000000000000001", "desc": "图文帖",
               "author": {"nickname": "测试博主"},
               "images": [{"url_list": ["https://p3-pc.douyinpic.com/img/a.jpg", "b.jpg"]},
                          {"url_list": ["https://p3-pc.douyinpic.com/img/c.jpg"]}]}
ev = m._douyin_aweme_to_entry(video_aweme, 1)
check("视频 url", ev["url"] == "https://www.douyin.com/video/7686046628160052393")
check("视频 media_type", ev["media_type"] == "video")
check("视频时长换算秒", ev["duration"] == 618)
check("视频带直链", ev["direct_url"].startswith("https://aweme.snssdk.com/aweme/v1/play/"))
check("视频带博主", ev["uploader"] == "测试博主")
ep = m._douyin_aweme_to_entry(photo_aweme, 2)
check("图文 url", ep["url"] == "https://www.douyin.com/photo/7500000000000000001")
check("图文 media_type", ep["media_type"] == "image_post")
check("图文 images 每条取首链", ep["images"] == ["https://p3-pc.douyinpic.com/img/a.jpg",
                                                  "https://p3-pc.douyinpic.com/img/c.jpg"])
check("图文无 duration/direct_url", "duration" not in ep and "direct_url" not in ep)

print("== 10. resolve_url 路由：user 链接进 CDP 枚举 ==")
called = {}
m.douyin_user_resolve = lambda u: (called.setdefault("url", u), [{"url": "x", "id": "1"}])[1]
e = m.resolve_url(f"https://www.douyin.com/user/{SEC}?vid=999")
check("进入 douyin_user_resolve", called.get("url", "").endswith(f"/user/{SEC}"), str(called))
check("返回枚举条目", len(e) == 1 and e[0]["id"] == "1")

subprocess.run, m.time.sleep, m.douyin_user_resolve = _ORIG

print()
if FAILED:
    print(f"❌ {len(FAILED)} 项失败：" + "; ".join(FAILED))
    sys.exit(1)
print("✅ 全部通过")
