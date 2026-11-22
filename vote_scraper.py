#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
vote_scraper.py —— 和平营地「巅峰挑战赛人气投票」票数定时采集

原理：截图 → OCR 读数字 → 坐标点击（退出/重进触发刷新）→ 追加 CSV

每轮动作：
  1. 若停在活动页，按 BACK 回到搜索页
  2. 点击搜索页横幅广告 → 进入活动页（WebView）
  3. 轮询截屏，连续两帧一致视为加载完成
  4. 下滑回顶部 → 截图 A（总票数 + 第 1 排：白小纯 / 青稚）
  5. 上滑 800px   → 截图 B（第 2 排：Paraboy / 33Svan）
  6. 两张图分别 OCR，按选手名锚点提取票数，合并
  7. 写 CSV，等 60 秒进入下一轮

为什么必须两段截图：页面底部有「投票记录 / 做任务赢 PEL」悬浮栏，
带渐变压暗，把第 2 排票数的下半截盖住，不滚动就永远读不准。

用法：
  python vote_scraper.py --dry-run     # 只截屏+OCR，不点击，用来校准
  python vote_scraper.py --once        # 完整跑一轮
  python vote_scraper.py               # 无限循环，默认 60 秒一轮
  python vote_scraper.py --interval 30

停止：Ctrl+C，或在脚本目录创建 .stop 文件
"""
import argparse
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
# 打包成 exe 后，资源会被解到临时目录，脚本无法再靠 __file__ 找同级文件
DATA_DIR = Path(os.environ.get("VOTE_TRACKER_HOME") or HERE)
SHOT_DIR = DATA_DIR / "shots"
CSV_PATH = DATA_DIR / "votes.csv"
STOP_FILE = DATA_DIR / ".stop"
CONFIG_PATH = DATA_DIR / "scraper_config.json"


def _bundle_dir():
    """打包成 exe 后，内置资源（adb 等）被解压到 PyInstaller 的临时目录"""
    return Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "frozen", False) else None


ADB_FILES = ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll")


def _install_bundled_adb():
    """把 exe 内置的 adb 和修复脚本释放到程序目录，返回 adb.exe 路径；没有内置则返回 None。

    释放到程序目录而不是留在临时解压目录：adb 会常驻一个后台 daemon，
    临时目录在主程序退出后被清空，daemon 就指向了不存在的路径。
    """
    b = _bundle_dir()
    if not b:
        return None

    # 华为/荣耀设备的 ADB 驱动修复脚本，一并放出来方便用户直接双击
    tools_src = b / "tools"
    if tools_src.is_dir():
        tools_dst = DATA_DIR / "tools"
        tools_dst.mkdir(parents=True, exist_ok=True)
        for f in tools_src.iterdir():
            if f.is_file():
                d = tools_dst / f.name
                if not (d.is_file() and d.stat().st_size == f.stat().st_size):
                    shutil.copy2(f, d)

    src = b / "platform-tools"
    if not (src / "adb.exe").is_file():
        return None
    dst = DATA_DIR / "platform-tools"
    dst.mkdir(parents=True, exist_ok=True)
    for name in ADB_FILES:
        s, d = src / name, dst / name
        if s.is_file() and not (d.is_file() and d.stat().st_size == s.stat().st_size):
            shutil.copy2(s, d)
    return dst / "adb.exe"


def find_adb():
    """按优先级查找 adb：环境变量 → 内置 → PATH → Android SDK → 程序同级目录"""
    exe_dir = Path(sys.executable).resolve().parent

    env = os.environ.get("ADB_PATH")
    if env and Path(env).is_file():
        return env

    bundled = _install_bundled_adb()
    if bundled:
        return str(bundled)

    cands = []
    w = shutil.which("adb")
    if w:
        cands.append(w)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cands.append(str(Path(local) / "Android" / "Sdk" / "platform-tools" / "adb.exe"))
    for base in (DATA_DIR, exe_dir):
        cands.append(str(base / "platform-tools" / "adb.exe"))
        cands.append(str(base / "platform-tools" / "adb"))
    cands += [
        r"C:\Android\platform-tools\adb.exe",
        r"C:\platform-tools\adb.exe",
        "/usr/local/bin/adb",
        "/usr/bin/adb",
    ]
    for c in cands:
        if c and Path(c).is_file():
            return c
    return "adb"  # 兜底，交给系统 PATH，找不到时由 subprocess 抛错


ADB = find_adb()

# ── 基准截图 1224x2688（真机原生分辨率）上的锚点，其他分辨率自动缩放 ──
BASE_W, BASE_H = 1224, 2688
DEFAULT_CFG = {
    # 搜索页：横幅广告「2026 PMC 最高人气由你来定」中心点
    "tap_ad": [612, 660],
    # 上滑：把第 2 排票数推到屏幕中部，避开底部悬浮栏
    "swipe_up": [612, 2100, 612, 1300, 500],
    # 下滑：回到页面顶部
    "swipe_down": [612, 1100, 612, 2300, 250],
    # 加载等待
    "load_timeout": 15.0,
    "stable_gap": 0.5,
    # 页面加载后额外静置，等异步数据渲染完
    "settle": 1.2,
    # 截图留档：只在 need 模式下落盘，裁掉无关区域后转 JPEG
    "crop_top": [0, 850, 1224, 1850],      # 总票数 + 第 1 排（白小纯/青稚）
    "crop_bottom": [0, 900, 1224, 1700],   # 第 2 排（Paraboy/33Svan）
    "jpeg_quality": 82,
}

# 选手名 → CSV 字段名。名字必须能在 OCR 结果里找到（作为锚点）
PLAYERS = [
    ("白小纯", "r1l"),
    ("青稚", "r1r"),
    ("Paraboy", "r2l"),
    ("33Svan", "r2r"),
]
ORDER = ["r1l", "r1r", "r2l", "r2r"]
HEADERS = {"total": "总票数", "r1l": "白小纯", "r1r": "青稚",
           "r2l": "Paraboy", "r2r": "33Svan"}

MIN_VOTE = 10_000          # 票数量级下限，用于过滤噪音
MAX_SANE_JUMP = 5_000_000  # 单轮增量上限，超过视为识别错误


def load_config():
    cfg = dict(DEFAULT_CFG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            print("配置读取失败，用默认值:", e)
    return cfg


# ──────────────────────────── ADB ────────────────────────────

class Adb:
    def __init__(self, serial=None):
        self.serial = serial

    def _args(self):
        return [ADB] + (["-s", self.serial] if self.serial else [])

    def run(self, *args, timeout=30, binary=False):
        try:
            r = subprocess.run(self._args() + list(args), capture_output=True, timeout=timeout)
        except FileNotFoundError:
            raise RuntimeError(
                "找不到 adb。三种解决办法：\n"
                "  1. 下载 Android SDK Platform-Tools，把整个 platform-tools 文件夹放到本程序同级目录\n"
                "  2. 把 platform-tools 加进系统 PATH\n"
                "  3. 设置环境变量 ADB_PATH 指向 adb.exe 的完整路径"
            )
        if r.returncode != 0:
            err = (r.stderr or b"").decode("utf-8", "ignore").strip()
            raise RuntimeError("adb %s 失败: %s" % (" ".join(args), err))
        return r.stdout if binary else r.stdout.decode("utf-8", "ignore")

    def ensure_device(self):
        out = self.run("devices")
        lines = [l.split() for l in out.splitlines()[1:] if l.strip()]
        lines = [p for p in lines if len(p) > 1 and p[1] == "device"]
        if not lines:
            raise RuntimeError(
                "没有已连接的设备。逐项检查：\n"
                "  1. 手机「开发者选项」里「USB 调试」已打开\n"
                "  2. 数据线支持传输（只能充电的线不行），插紧\n"
                "  3. 手机上弹出的「允许 USB 调试」已点允许\n"
                "  4. 华为/荣耀机型如果前三条都对、列表仍然为空，\n"
                "     双击本目录下的 tools\\fix_adb_driver.bat 修复驱动，然后重试"
            )
        if self.serial is None:
            # 优先选 USB 直连（序列号是纯设备号），无线调试的条目会带 _adb-tls-connect 这类后缀
            def rank(p):
                s = p[0]
                wireless = ("_adb-tls" in s) or ("." in s) or (":" in s)
                return (wireless, s)
            self.serial = sorted(lines, key=rank)[0][0]
        for parts in lines:
            if parts[0] == self.serial:
                return self.serial
        raise RuntimeError("找不到设备 %s" % self.serial)

    def size(self):
        out = self.run("shell", "wm", "size")
        m = re.findall(r"(\d+)x(\d+)", out)
        if not m:
            raise RuntimeError("读不到分辨率: %r" % out)
        return int(m[-1][0]), int(m[-1][1])

    def tap(self, x, y):
        self.run("shell", "input", "tap", str(int(x)), str(int(y)))

    def swipe(self, x1, y1, x2, y2, dur):
        self.run("shell", "input", "swipe",
                 str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(int(dur)))

    def back(self):
        self.run("shell", "input", "keyevent", "KEYCODE_BACK")

    def _try(self, *args):
        """执行但不因失败抛错，用于尽力而为的辅助命令"""
        try:
            self.run(*args, timeout=15)
            return True
        except Exception:
            return False

    def wake(self):
        self.run("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        self._try("shell", "wm", "dismiss-keyguard")
        # 只在上锁时才滑动，避免在活动页面上误触把页面滚走
        if self.keyguard_showing():
            try:
                w, h = self.size()
                self._try("shell", "input", "swipe", str(w // 2), str(int(h * 0.85)),
                          str(w // 2), str(int(h * 0.35)), "260")
            except Exception:
                pass
        self._try("shell", "svc", "power", "stayon", "true")
        # 关掉自动息屏，挂机采集期间屏幕必须一直亮着
        self._try("shell", "settings", "put", "system", "screen_off_timeout", "1800000")

    def keyguard_showing(self):
        out = self.run("shell", "dumpsys", "window", timeout=20)
        return "isKeyguardShowing=true" in out

    def top_activity(self):
        out = self.run("shell", "dumpsys", "activity", "activities", timeout=20)
        m = re.search(r"topResumedActivity=.*?\s(\S+)/(\S+)\s", out)
        return (m.group(1), m.group(2)) if m else (None, None)

    def screencap(self):
        for attempt in (1, 2):
            data = self.run("exec-out", "screencap", "-p", timeout=40, binary=True)
            if len(data) >= 2000:
                return Image.open(io.BytesIO(data)).convert("RGB")
            # 锁屏状态下部分机型（华为等）会直接拒绝截屏，唤醒后再试一次
            if attempt == 1:
                self.wake()
                time.sleep(0.8)
        if self.keyguard_showing():
            raise RuntimeError(
                "截屏失败：手机处于锁屏状态。\n"
                "  华为等机型在锁屏时会拒绝截屏，ADB 也无法绕过密码锁。\n"
                "  请手动解锁手机并保持屏幕点亮，下一轮会自动恢复。"
            )
        raise RuntimeError("截屏失败，返回内容异常（%d 字节）" % len(data))


# ──────────────────────────── 画面指纹 ────────────────────────────

def dhash(img, size=8):
    g = img.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(g.getdata())
    bits = 0
    for row in range(size):
        base = row * (size + 1)
        for col in range(size):
            bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
    return bits


def hamming(a, b):
    return bin(a ^ b).count("1")


# ──────────────────────────── OCR ────────────────────────────

class Ocr:
    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR
        self.engine = RapidOCR()
        self.calls = 0

    def read(self, img):
        """返回 [(text, cx, cy, score), ...]"""
        import numpy as np
        self.calls += 1
        out, _ = self.engine(np.array(img))
        items = []
        if not out:
            return items
        for box, text, score in out:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            items.append((str(text).strip(), sum(xs) / 4, sum(ys) / 4, float(score)))
        return items


NUM_RE = re.compile(r"\d{1,3}(?:[,\s，]\d{3})+|\d{5,}")
TOTAL_RE = re.compile(r"总投票?数|总票数")


def to_int(s):
    return int(re.sub(r"\D", "", s))


def extract_total(items):
    """从「所有玩家总投票数：13758372」里取数字

    OCR 有时会把标签和数字拆成两条（「所有玩家总投票数：」+「20391483」），
    只认同一条里的数字会漏读，所以再退一步：在同一横带里找靠右的那个数。
    """
    label_y = None
    for text, cx, cy, score in items:
        if TOTAL_RE.search(text):
            tail = re.split(r"[:：]", text)[-1]
            m = NUM_RE.search(tail)
            if m and to_int(m.group()) >= MIN_VOTE:
                return to_int(m.group())
            if label_y is None:
                label_y = cy
    if label_y is None:
        return None
    best = None
    for text, cx, cy, score in items:
        if abs(cy - label_y) > 60:
            continue
        m = NUM_RE.search(text)
        if not m:
            continue
        v = to_int(m.group())
        if v < MIN_VOTE:
            continue
        if best is None or cx > best[1]:
            best = (v, cx)
    return best[0] if best else None


def find_anchor(items, name):
    """定位选手名，返回 (cx, cy)"""
    for text, cx, cy, score in items:
        if name.lower() in text.lower():
            return cx, cy
    return None


def vote_candidates(items, ax, ay):
    """名字下方、同一张卡片内的票数候选 [(值, 置信度), ...]"""
    cands = []
    for text, cx, cy, score in items:
        if not (ay + 25 <= cy <= ay + 180):
            continue
        # 票数必须和名字在同一张卡片里。原先按「左半屏 / 右半屏」判断是错的：
        # 第 2 排左列其实是 33Svan、右列是 Paraboy，和 key 里的 l/r 假设相反，
        # 结果这两人的票数一直互换。改成看横向距离，与页面怎么排无关。
        if abs(cx - ax) > 300:
            continue
        m = NUM_RE.search(text)
        if not m:
            continue
        v = to_int(m.group())
        if v < MIN_VOTE:
            continue
        cands.append((v, score))
    return cands


def tile_candidates(items):
    """一小块裁剪图里的所有数字候选（图里只有一张卡片的票数）"""
    cands = []
    for text, cx, cy, score in items:
        m = NUM_RE.search(text)
        if not m:
            continue
        v = to_int(m.group())
        if v < MIN_VOTE:
            continue
        cands.append((v, score))
    return cands


def plausible(v, p, minutes):
    """票数只增不减，且单轮涨幅有上限 —— OCR 少读几位全靠这一条挡

    每分钟最多涨 1%，再加 5 万缓冲：断采很久也够宽松，一分钟内的错读一定挡得住。
    """
    if v is None:
        return False
    if not p:
        return True
    grow = 0.01 * max(minutes, 1.0)
    return p * (1 - 0.02 - grow) <= v <= p * (1 + grow) + 50000


def pick_vote(cands, prev=None, minutes=1.0):
    """在候选里挑一个落在合理区间的；全都不合理就返回 None"""
    if not cands:
        return None
    if not prev:
        return max(cands, key=lambda c: c[1])[0]
    grow = 0.01 * max(minutes, 1.0)
    lo, hi = prev * (1 - 0.02 - grow), prev * (1 + grow) + 50000
    ok = [c for c in cands if lo <= c[0] <= hi]
    return max(ok, key=lambda c: c[1])[0] if ok else None


def extract_votes(items, w, h, prev=None, minutes=1.0):
    """按选手名做锚点，取其下方 25~180px、同一张卡片内的票数

    prev / minutes 用来挡识别错误：OCR 偶尔会把 6,086,956 读成 86,956（丢高位），
    这类值置信度不一定低，光靠 score 挑会挑错，必须用上一轮的值卡区间。
    """
    result = {"total": extract_total(items)}
    for name, key in PLAYERS:
        a = find_anchor(items, name)
        if not a:
            result.setdefault(key, None)
            continue
        result[key] = pick_vote(vote_candidates(items, a[0], a[1]),
                                (prev or {}).get(key), minutes)
    return result


def merge(*dicts):
    out = {}
    for d in dicts:
        for k, v in d.items():
            if v is not None:
                out[k] = v
    return out


# ──────────────────────────── 主流程 ────────────────────────────

class Scraper:
    def __init__(self, cfg, interval=60.0, save_mode="need"):
        self.cfg = cfg
        self.interval = interval
        # save_mode: none 不存 / need 只在读数可疑时存 / all 每轮都存
        self.save_mode = save_mode
        self.last_images = None
        self.prev = read_last_known()   # 上一轮读数，用来卡「票数只能涨」的合理区间；重启时从 CSV 续上
        self.gap_min = 1.0      # 距上一轮采样的分钟数，断采越久允许的涨幅越大
        self.last_t = None
        self.dev = Adb()
        # 先确定设备再读分辨率：同时插着线又开着无线调试时，不带 -s 的 adb 命令会拒绝执行
        serial = self.dev.ensure_device()
        self.w, self.h = self.dev.size()
        self.ocr = Ocr()
        print("设备: %s   分辨率: %dx%d   留档模式: %s" % (
            serial, self.w, self.h, save_mode))

    def pt(self, pair):
        return pair[0] * self.w / BASE_W, pair[1] * self.h / BASE_H

    def swipe_cfg(self, key):
        x1, y1, x2, y2, dur = self.cfg[key]
        (x1, y1), (x2, y2) = self.pt((x1, y1)), self.pt((x2, y2))
        self.dev.swipe(x1, y1, x2, y2, dur)

    def wait_loaded(self):
        """连续两帧指纹一致 → 认为页面加载完成"""
        cfg = self.cfg
        deadline = time.monotonic() + cfg["load_timeout"]
        prev = None
        last = None
        while time.monotonic() < deadline:
            img = self.dev.screencap()
            cur = dhash(img)
            last = img
            if prev is not None and hamming(prev, cur) <= 2:
                time.sleep(cfg["settle"])
                return self.dev.screencap()
            prev = cur
            time.sleep(cfg["stable_gap"])
        print("  ! 加载超时，用最后一帧")
        return last

    def capture_pair(self):
        """截图 A（顶部）+ 上滑后截图 B（第 2 排）"""
        self.swipe_cfg("swipe_down")      # 无论停在哪儿，先回顶部
        time.sleep(0.5)
        a = self.dev.screencap()
        self.swipe_cfg("swipe_up")
        time.sleep(0.5)
        b = self.dev.screencap()
        return a, b

    def enter_activity(self):
        """保证从搜索页点广告进入活动页"""
        pkg, act = self.dev.top_activity()
        if act and "WebViewActivity" in act:
            self.dev.back()               # 已在活动页，先退回搜索页
            time.sleep(1.0)
        ax, ay = self.pt(self.cfg["tap_ad"])
        self.dev.tap(ax, ay)

    def sanitize(self, votes):
        """写入前的最后一道：落在合理区间外的值一律清空

        这里**不抄上一轮的值**。抄的后果是这一轮 Δ=0、下一轮暴涨把差额补回来，
        曲线上会出现一个假的平段加一个假尖峰 —— 比留个空值更难解释。
        空值在看板上显示为「—」，下一轮自动按真实间隔折算，数据不会失真。
        """
        if not self.prev:
            return votes
        out = dict(votes)
        bad = []
        for k in ORDER:
            p = self.prev.get(k)
            if not p:
                continue
            v = out.get(k)
            if not plausible(v, p, self.gap_min):
                out[k] = None
                bad.append(k)
        if bad:
            print("  ! %s 读数不可信，本轮留空" %
                  "、".join(HEADERS[k] for k in bad))
        return out

    @staticmethod
    def _variants(tile):
        """同一块数字的几种预处理，逐个喂给 OCR"""
        from PIL import ImageOps
        yield tile
        g = ImageOps.autocontrast(tile.convert("L"), cutoff=1)
        yield g.convert("RGB")
        yield ImageOps.invert(g).convert("RGB")

    def refine_vote(self, blocks, key, prev):
        """整块读不准时，单独裁这张卡片下方的数字再读一遍

        整块裁剪图里两张大卡片并排、数字偏小，偶发丢高位；单独裁一小块让数字占满画面，
        识别率明显更高。几种预处理各试一遍，取第一个落在合理区间的读数。
        """
        name = next(n for n, k in PLAYERS if k == key)
        for img, items in blocks:
            a = find_anchor(items, name)
            if not a:
                continue
            ax, ay = a
            x0, y0 = max(0, int(ax - 300)), max(0, int(ay + 15))
            x1, y1 = min(img.width, int(ax + 300)), min(img.height, int(ay + 180))
            if x1 - x0 < 120 or y1 - y0 < 50:
                continue
            tile = img.crop((x0, y0, x1, y1))
            for var in self._variants(tile):
                got = pick_vote(tile_candidates(self.ocr.read(var)), prev, self.gap_min)
                if got is not None:
                    return got
        return None

    def refine_total(self, blocks, prev):
        """总票数那一行没读到时，只裁这一条横带重读

        整块里 1000px 高的内容 OCR 偶尔会把这一行整条漏掉；单独裁出来几乎必中。
        """
        for img, items in blocks:
            ys = [cy for text, cx, cy, score in items if TOTAL_RE.search(text)]
            y0 = int(ys[0]) - 70 if ys else 110
            band = img.crop((0, max(0, y0), img.width, min(img.height, y0 + 130)))
            for var in self._variants(band):
                got = pick_vote(tile_candidates(self.ocr.read(var)), prev, self.gap_min)
                if got:
                    return got
        return None

    @staticmethod
    def suspect_fields(votes):
        """缺失，或比上一名次小 3 倍以上 → 判为识别可疑"""
        bad = []
        prev = None
        for k in ORDER:
            v = votes.get(k)
            if v is None:
                bad.append(k)
            elif prev and v < prev / 3:
                bad.append(k)
            if v is not None:
                prev = v
        return bad

    def crop(self, img, tag):
        """裁出关键区域。OCR 只跑这一块：字更大、页面其余内容不干扰，识别率比整屏高得多"""
        box = self.cfg.get("crop_top" if tag == "top" else "crop_bottom")
        if not box:
            return img
        sx, sy = img.width / BASE_W, img.height / BASE_H
        x0, y0, x1, y1 = box
        return img.crop((int(x0 * sx), int(y0 * sy), int(x1 * sx), int(y1 * sy)))

    def save_shot(self, img, tag, full=False):
        """裁剪关键区域后存成 JPEG，单张约 100KB（全屏 PNG 是 2.4MB）"""
        SHOT_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = img
        if not full:
            box = self.cfg.get("crop_top" if tag == "top" else "crop_bottom")
            if box:
                sx, sy = img.width / BASE_W, img.height / BASE_H
                x0, y0, x1, y1 = box
                out = img.crop((int(x0 * sx), int(y0 * sy),
                                int(x1 * sx), int(y1 * sy)))
        path = SHOT_DIR / ("%s_%s.jpg" % (tag, ts))
        out.save(path, "JPEG", quality=self.cfg["jpeg_quality"], optimize=True)
        return path

    def read_once(self, dry_run=False, save=False):
        if dry_run:
            a, b = self.capture_pair()
        else:
            self.enter_activity()
            self.wait_loaded()
            a, b = self.capture_pair()

        ta, tb = self.crop(a, "top"), self.crop(b, "bottom")
        ia, ib = self.ocr.read(ta), self.ocr.read(tb)
        va = extract_votes(ia, ta.width, ta.height,
                           prev=self.prev, minutes=self.gap_min)
        vb = extract_votes(ib, tb.width, tb.height,
                           prev=self.prev, minutes=self.gap_min)
        votes = merge(va, vb)

        # 总票数读不到就整轮作废，损失太大 —— 单独裁这一条横带再试
        if votes.get("total") is None:
            got = self.refine_total([(ta, ia), (tb, ib)], self.prev.get("total"))
            if got is not None:
                votes["total"] = got
                print("    · 总票数 重读成功 %d" % got)

        # 缺字段、或落在合理区间外的，单独裁这张卡片重读一次
        for k in [k for k in ORDER
                  if not plausible(votes.get(k), self.prev.get(k), self.gap_min)]:
            got = self.refine_vote([(ta, ia), (tb, ib)], k, self.prev.get(k))
            if got is None:
                print("    · %s 重读失败，本轮留空" % HEADERS[k])
            else:
                print("    · %s 重读成功 %d" % (HEADERS[k], got))
                votes[k] = got
        self.last_images = (a, b)   # 供 run() 在读数可疑时回捞留档

        # OCR 已经做完，压缩对结果零影响
        if save:
            self.save_shot(a, "top")
            self.save_shot(b, "bottom")
        return votes, {"top": va, "bottom": vb}

    def run(self, rounds=0, dry_run=False):
        n = 0
        fails = 0
        t_next = time.monotonic()
        while True:
            n += 1
            if rounds and n > rounds:
                break
            if STOP_FILE.exists():
                print("检测到 .stop，退出")
                break

            t0 = time.monotonic()
            if self.last_t:
                self.gap_min = max((t0 - self.last_t) / 60, 0.5)
            self.last_t = t0
            try:
                votes, raw = self.read_once(dry_run=dry_run,
                                            save=(self.save_mode == "all"))
                votes = self.sanitize(votes)
                # 一个数字都没读到（页面没加载完 / 截图是白屏）就跳过，别往 CSV 里写空行
                if votes.get("total") is None:
                    raise RuntimeError("本轮没读到总票数，跳过写入")
                bad = self.suspect_fields(votes)
                if bad and self.save_mode == "need" and self.last_images:
                    a, b = self.last_images
                    self.save_shot(a, "suspect_top")
                    self.save_shot(b, "suspect_bottom")
                    print("  ~ 已留档可疑轮截图")
                print("[%s] %s   用时 %.1fs%s" % (
                    datetime.now().strftime("%H:%M:%S"),
                    "  ".join("%s=%s" % (HEADERS.get(k, k), v) for k, v in
                              [("total", votes.get("total"))] +
                              [(k, votes.get(k)) for k in ORDER]),
                    time.monotonic() - t0,
                    ("  ！可疑 " + ",".join(bad)) if bad else ""))
                self.append_csv(votes)
                # 留空的列不覆盖已知值：下一轮还要拿它当参照
                self.prev = {k: (votes.get(k) if votes.get(k) is not None
                                 else (self.prev or {}).get(k))
                             for k in ["total"] + ORDER}
            except KeyboardInterrupt:
                raise
            except Exception as e:
                fails += 1
                print("[%s] 本轮失败: %s" % (datetime.now().strftime("%H:%M:%S"), e))
                if fails >= 3:
                    print("")
                    print("  !! 已连续 %d 轮失败。" % fails)
                    print("     最常见的原因是手机锁屏 —— 请解锁手机，别让它自动息屏。")
                    print("     其次是数据线松动，拔插一次即可。程序每轮都会自动重试，不用重启。")
                    print("")
            else:
                if fails >= 3:
                    print("[%s] 已恢复" % datetime.now().strftime("%H:%M:%S"))
                fails = 0

            if dry_run:
                break
            if rounds and n >= rounds:
                break
            t_next += self.interval
            left = t_next - time.monotonic()
            if left > 0:
                time.sleep(left)
            else:
                t_next = time.monotonic()

    def append_csv(self, v):
        prev = read_last_csv()
        total = v.get("total")
        d_total = (total - prev["total"]) if (prev and total and prev.get("total")) else None
        d_sum = None
        if prev:
            diffs = [v[k] - prev[k] for k in ORDER if v.get(k) and prev.get(k)]
            if diffs:
                d_sum = sum(diffs)
        share = None
        if d_total and d_sum is not None and d_total > 0:
            share = round(d_sum / d_total * 100, 2)

        new = not CSV_PATH.exists()
        with CSV_PATH.open("a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["时间", "总票数", HEADERS["r1l"], HEADERS["r1r"],
                            HEADERS["r2l"], HEADERS["r2r"],
                            "Δ总", "四人Δ合计", "四人Δ占比%"])
            w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        total, v.get("r1l"), v.get("r1r"), v.get("r2l"), v.get("r2r"),
                        d_total, d_sum, share])


def read_last_known(max_rows=10):
    """逐列向前找最近一个非空值，供启动时卡区间用（末行可能有意留空的列）"""
    if not CSV_PATH.exists():
        return {}
    try:
        with CSV_PATH.open("r", newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
    except Exception:
        return {}
    keys = ["total"] + ORDER
    out = {}
    for row in reversed(rows[1:][-max_rows:]):
        for i, k in enumerate(keys):
            if k in out:
                continue
            try:
                out[k] = int(row[i + 1])
            except (ValueError, IndexError):
                pass
        if len(out) == len(keys):
            break
    return out


def read_last_csv():
    if not CSV_PATH.exists():
        return None
    try:
        with CSV_PATH.open("r", newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        if len(rows) < 2:
            return None
        last = rows[-1]
        keys = ["total"] + ORDER
        out = {}
        for i, k in enumerate(keys):
            try:
                out[k] = int(last[i + 1])
            except (ValueError, IndexError):
                out[k] = None
        return out
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--rounds", type=int, default=0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--save", choices=["none", "need", "all"], default="need",
                    help="截图留档：none 不存 / need 只在读数可疑时存（默认）/ all 每轮都存")
    args = ap.parse_args()

    cfg = load_config()
    if STOP_FILE.exists():
        STOP_FILE.unlink()

    s = Scraper(cfg, args.interval, args.save)
    if not args.dry_run:
        s.dev.wake()

    if args.dry_run:
        votes, raw = s.read_once(dry_run=True, save=True)
        print("\n=== 顶部截图解析 ===")
        print(json.dumps(raw["top"], ensure_ascii=False))
        print("\n=== 滚动后截图解析 ===")
        print(json.dumps(raw["bottom"], ensure_ascii=False))
        print("\n=== 合并结果 ===")
        print(json.dumps(votes, ensure_ascii=False, indent=2))
        print("\n截图保存在 shots/（裁切后 JPEG）")
        return

    rounds = 1 if args.once else args.rounds
    s.run(rounds=rounds)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已停止")
    except Exception as e:
        print("启动失败:", e)
        sys.exit(1)
