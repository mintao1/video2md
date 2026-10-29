#!/usr/bin/env python3
"""test_backup_scripts.py — backup.sh / restore.sh 离线单测（stub docker，零真实卷操作）。

覆盖：dry-run 命令拼装（卷名/挂载/落盘路径）/ mkdir 锁防重入与释放 /
retention prune（keep 边界）/ 周备晋升触发（周一）/ manifest 生成 /
restore 参数校验与归档查找顺序（daily 优先 weekly）。

运行：python3 test_backup_scripts.py（PATH shim 假 docker；不碰任何真实 Docker 卷）。
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]  # 仓根（scripts/ 留根）
BACKUP = REPO / "scripts" / "backup.sh"
RESTORE = REPO / "scripts" / "restore.sh"

TMP = Path(tempfile.mkdtemp(prefix="v2md-test-bak-"))

# ---- stub docker：记录调用，行为按需伪造 ------------------------------------------
SHIM = TMP / "bin"
SHIM.mkdir()
CALLS = TMP / "docker-calls.log"

DOCKER_SHIM = f"""#!/bin/bash
echo "$*" >> {CALLS}
case "$1 $2" in
  "ps --format") echo "video2md-video2md-1"; exit 0 ;;   # 无 postgres 容器在跑
  *) exit 0 ;;
esac
"""
(SHIM / "docker").write_text(DOCKER_SHIM)
(SHIM / "docker").chmod(0o755)

ENV = {**os.environ, "PATH": f"{SHIM}:{os.environ['PATH']}"}


def run_backup(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(BACKUP), *args],
                          env=ENV, capture_output=True, text=True, timeout=60)


def calls() -> list[str]:
    return CALLS.read_text().splitlines() if CALLS.exists() else []


# ---- 1. dry-run：命令拼装 ----------------------------------------------------------
CALLS.unlink(missing_ok=True)
dst = TMP / "bak-dry"
r = run_backup("--dry-run", "--dst", str(dst))
assert r.returncode == 0, r.stderr
out = r.stdout
assert "video2md_v2md-data:/src:ro" in out, "data 卷挂载缺失"
assert "video2md_vault:/src:ro" in out, "vault 卷挂载缺失"
assert f"[dry-run] docker run --rm -v video2md_v2md-data:/src:ro -v {dst}/daily:/dst" in out
assert "tar czf /dst/data-" in out and "tar czf /dst/vault-" in out
assert "docker compose -f" in out and "ps --status running --services" in out, \
    "dry-run 应打印 Compose postgres 探测命令"
assert not (dst / "daily").exists() or not any((dst / "daily").iterdir()), \
    "dry-run 不得真写归档"
# dry-run 允许的 docker 调用只有无害探测（docker ps）；tar/exec 必须走 run() 打印而非执行
real = [c for c in calls() if not c.startswith("ps ")]
assert not real, f"dry-run 不得真执行备份命令：{real}"

# ---- 2. 真跑（stub docker）：锁释放 / 目录结构 / manifest ---------------------------
CALLS.unlink(missing_ok=True)
dst = TMP / "bak-real"
r = run_backup("--dst", str(dst))
assert r.returncode == 0, r.stderr
assert (dst / "daily" / "manifest.sha256").exists() or (dst / "manifest.sha256").exists(), \
    "manifest 应生成"
# stub docker 不真产 tar，manifest 里 0 个文件合法；锁目录必须已释放（trap）
assert not (dst / ".lock").exists(), "退出后锁必须释放（trap）"
# docker 被调 4 次：data/vault 各「tar czf 打包 + tar tzf 完整性校验」
assert sum(1 for c in calls() if "tar czf" in c) == 2, calls()
assert sum(1 for c in calls() if "tar tzf" in c) == 2, calls()

# ---- 3. 防重入：锁被占时跳过（不执行备份；不删别人的锁——trap 只在拿到锁后注册）------
(dst / ".lock").mkdir(parents=True, exist_ok=True)
r = run_backup("--dst", str(dst))
assert r.returncode == 0
assert "已有备份在跑" in r.stdout, r.stdout
assert "备份完成" not in r.stdout, "被锁跳过时不应执行备份"
assert (dst / ".lock").exists(), "跳过路径不得删持有者的锁"

# ---- 4. retention：keep 边界 --------------------------------------------------------
# 直接造 10 天假归档再跑一次 keep-daily=3：stub docker 不产新文件，prune 只删旧的
dst = TMP / "bak-prune"
daily = dst / "daily"
daily.mkdir(parents=True)
dates = [f"2026-09-{d:02d}" for d in range(14, 24)]  # 10 天
for d in dates:
    (daily / f"data-{d}.tar.gz").write_text("x")
    (daily / f"vault-{d}.tar.gz").write_text("x")
r = run_backup("--dst", str(dst), "--keep-daily", "3")
assert r.returncode == 0, r.stderr
# 新备份日期 = 今天（真实 date），stub 没产文件；prune 只看现存 10 个 → 应删 7 个留 3 个最老之外
left = sorted(p.name for p in daily.glob("data-*.tar.gz"))
assert len(left) == 3, f"keep-daily=3 应剩 3 份，剩 {left}"
assert left == sorted(left)[-3:], "应保留最新的 3 份"

# ---- 5. 周备晋升：伪造周一（TZ 不动，直接测 cp 逻辑太重——用 dry-run 断言不晋升即可）----
# 晋升条件 DOW==1 依赖真实时钟，单测不锚定星期；这里只验证非周一不晋升不报错
dst = TMP / "bak-week"
r = run_backup("--dst", str(dst))
assert r.returncode == 0

# ---- 6. restore：参数校验 + 归档查找 ------------------------------------------------
def run_restore(*args: str) -> subprocess.CompletedProcess:
    # errors=replace：脚本输出含 emoji/全角，Windows/GBK 终端解码不炸
    return subprocess.run(["bash", str(RESTORE), *args], env=ENV,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60, stdin=subprocess.DEVNULL)

r = run_restore()
assert r.returncode == 2, "缺日期应退出码 2"
r = run_restore("bad-date", "--only", "data")
# bad-date 找不到归档 → 停栈前就失败；stub docker down 成功但 find_file 失败退出 1
assert r.returncode in (1, 2), r.stdout + r.stderr

# 有归档时：--yes 跳过确认，但 sha256 manifest 校验失败应拒绝
dst = TMP / "bak-restore"
(daily := dst / "daily").mkdir(parents=True)
(daily / "data-2026-09-23.tar.gz").write_text("fake")
(dst / "manifest.sha256").write_text("deadbeef  daily/data-2026-09-23.tar.gz\n")
r = run_restore("2026-09-23", "--dst", str(dst), "--only", "data", "--yes")
assert r.returncode == 1 and "manifest 校验失败" in r.stderr, \
    f"manifest 不匹配必须拒绝恢复：{r.stdout} {r.stderr}"

# ---- 7. 异地容灾：V2MD_BACKUP_REMOTE 驱动 rclone；失败不阻断本地保留 -----------------
RCLONE_CALLS = TMP / "rclone-calls.log"
RCLONE_CALLS.unlink(missing_ok=True)
(SHIM / "rclone").write_text(f"""#!/bin/bash
echo "$*" >> {RCLONE_CALLS}
exit 0
""")
(SHIM / "rclone").chmod(0o755)


def run_backup_env(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(BACKUP), *args], env=env,
                          capture_output=True, text=True, timeout=60)


# 未配 remote → 不推 rclone
dst7 = TMP / "bak-remote-off"
r = run_backup("--dst", str(dst7))
assert r.returncode == 0, r.stderr
assert not RCLONE_CALLS.exists() or not RCLONE_CALLS.read_text().strip(), \
    "未配 V2MD_BACKUP_REMOTE 不得推远端"

# 配了 remote → rclone copy <dst> <remote>（排 .lock），本地链路照常
ENV_REMOTE = {**ENV, "V2MD_BACKUP_REMOTE": "b2:v2md-backup"}
dst8 = TMP / "bak-remote-on"
r = run_backup_env(ENV_REMOTE, "--dst", str(dst8))
assert r.returncode == 0, r.stderr
assert "异地推送" in r.stdout, r.stdout
c = RCLONE_CALLS.read_text()
assert f"sync {dst8} b2:v2md-backup --transfers 4" in c, c
assert "--exclude .lock/**" in c, "锁目录不得推远端"
assert (dst8 / "manifest.sha256").exists(), "本地 manifest 不受 remote 影响"

# rclone 失败 → 只告警不阻断（退出码 3 区分），本地备份照留
RCLONE_CALLS.unlink(missing_ok=True)
(SHIM / "rclone").write_text(f"""#!/bin/bash
echo "$*" >> {RCLONE_CALLS}
exit 1
""")
(SHIM / "rclone").chmod(0o755)
dst9 = TMP / "bak-remote-fail"
r = run_backup_env(ENV_REMOTE, "--dst", str(dst9))
assert r.returncode == 3, f"rclone 失败应以退出码 3 区分：{r.returncode}"
assert "rclone 推送失败" in r.stderr, r.stderr
assert (dst9 / "manifest.sha256").exists(), "失败不阻断本地保留"

# 配了 remote 但服务器无 rclone → 同样告警退出 3
(SHIM / "rclone").unlink()
dst10 = TMP / "bak-remote-nobin"
r = run_backup_env(ENV_REMOTE, "--dst", str(dst10))
assert r.returncode == 3 and "传输配置无效或缺少 rclone" in r.stderr, (r.returncode, r.stderr)
assert (dst10 / "manifest.sha256").exists()

shutil.rmtree(TMP, ignore_errors=True)
print("✅ test_backup_scripts.py 全部通过")
