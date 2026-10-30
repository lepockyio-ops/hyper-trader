"""事件日历：从美国官方来源抓取重要宏观事件，写成交易程序读取的 events.json。
Hyperliquid 版与 freqtrade 版共用同一份文件（freqtrade 版：monitor/events_calendar.py；Hyperliquid 版：s03/events_calendar.py）。只依赖标准库。

来源（全部免费、官方、无需 API Key）：
  美联储 FOMC 日程页面    https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
      → 利率决议：会议最后一天 14:00（美东）；主席新闻发布会：14:30（美东）
  美国劳工统计局 BLS 日历  https://www.bls.gov/schedule/news_release/bls.ics
      → CPI、非农（Employment Situation）、PPI，08:30（美东）
      BLS 要求自动程序在 User-Agent 里写联系方式，否则拒绝访问：用环境变量 S03_EVENTS_CONTACT 提供
  美国经济分析局 BEA 日历  https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics
      → GDP 各次估计、个人收入与支出（PCE），08:30（美东）

用法：
  python -m s03.events_calendar --out /var/lib/s03-events/events.json            # 未来 35 天
  python -m s03.events_calendar --out research_events.json --history            # 来源里的全部历史（研究用）
退出码：0 = 全部来源正常；1 = 有来源失败或检查未通过（保留该来源上一版数据，并让 systemd 唤醒 s03-ops）；2 = 无法写文件
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

SOURCES = {
    "fed": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    "bls": "https://www.bls.gov/schedule/news_release/bls.ics",
    "bea": "https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics",
}

# 白名单：(来源, 标题正则, 事件类别)。只有这些事件会进入事件窗口；按需增删。
WHITELIST = [
    ("bls", r"^Consumer Price Index$", "US_CPI"),
    ("bls", r"^Employment Situation$", "US_NFP"),
    ("bls", r"^Producer Price Index$", "US_PPI"),
    ("bea", r"^GDP \((Advance|Second|Third) Estimate\)", "US_GDP"),
    ("bea", r"^Personal Income and Outlays", "US_PCE"),
]

MONTHS = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August",
                                       "September", "October", "November", "December"], start=1)}


# ---------------------------------------------------------------------------- 抓取
def user_agent() -> str:
    contact = os.environ.get("S03_EVENTS_CONTACT", "").strip()
    return f"s03-events/1.0 (personal research; contact: {contact or 'unset'})"


def fetch(url: str, timeout: float = 30.0, tries: int = 3) -> str:
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": user_agent()})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                text = r.read().decode("utf-8", errors="replace")
            if "Access Denied" in text[:3000] and "BEGIN:VCALENDAR" not in text[:200]:
                raise PermissionError("来源拒绝访问（BLS 需要在 S03_EVENTS_CONTACT 里提供联系方式）")
            return text
        except Exception as e:                         # 网络错误重试，退避 5、10 秒
            last = e
            time.sleep(5 * (i + 1))
    raise last  # type: ignore[misc]


# ---------------------------------------------------------------------------- 解析
def _event(source: str, kind: str, title: str, t_utc: datetime) -> dict:
    iso = t_utc.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    eid = hashlib.sha256(f"{source}|{kind}|{title}|{iso[:10]}".encode()).hexdigest()[:16]
    return {"id": eid, "source": source, "kind": kind, "title": title, "time_utc": iso}


def parse_fomc(html: str) -> list[dict]:
    """解析 FOMC 日程页。每次会议产出“利率决议”和“新闻发布会”两个事件（会议最后一天，美东 14:00 / 14:30）。
    跨月会议（如 “Oct/Nov 31-1”）取后一个月；“(notation vote)”、取消的会议跳过。"""
    out: list[dict] = []
    for ym in re.finditer(r"(\d{4}) FOMC Meetings", html):
        year = int(ym.group(1))
        nxt = re.search(r"\d{4} FOMC Meetings", html[ym.end():])
        block = html[ym.end(): ym.end() + nxt.start()] if nxt else html[ym.end():]
        for m in re.finditer(r'fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>\s*</div>\s*'
                             r'<div class="fomc-meeting__date[^>]*>([^<]+)</div>', block):
            month_txt, day_txt = m.group(1).strip(), m.group(2).strip()
            if "notation" in day_txt.lower() or "cancel" in day_txt.lower() or "unscheduled" in day_txt.lower():
                continue
            last_month = month_txt.split("/")[-1].strip()
            if last_month not in MONTHS:
                continue
            days = re.findall(r"\d+", day_txt)
            if not days:
                continue
            d = date(year, MONTHS[last_month], int(days[-1]))
            decision = datetime(d.year, d.month, d.day, 14, 0, tzinfo=ET)
            out.append(_event("fed", "FOMC_DECISION", f"FOMC rate decision ({month_txt} {day_txt})", decision))
            out.append(_event("fed", "FOMC_PRESS", f"FOMC press conference ({month_txt} {day_txt})",
                              decision + timedelta(minutes=30)))
    return out


def _unfold(ics: str) -> str:
    return re.sub(r"\r?\n[ \t]", "", ics)


def _ics_time(prop: str, value: str) -> datetime | None:
    """支持 DTSTART:...Z、DTSTART;TZID=US-Eastern:...、DTSTART;VALUE=DATE-TIME:...Z。无时区的按美东处理。"""
    value = value.strip()
    try:
        if value.endswith("Z"):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        if "T" not in value:
            return None                                # 全天事件没有时间，不用于事件窗口
        dt = datetime.strptime(value, "%Y%m%dT%H%M%S")
        tzid = re.search(r"TZID=([^;:]+)", prop)
        tz = ET if (tzid is None or "East" in tzid.group(1) or "New_York" in tzid.group(1)) else ZoneInfo(tzid.group(1))
        return dt.replace(tzinfo=tz)
    except (ValueError, KeyError):
        return None


def parse_ics(ics: str, source: str) -> list[dict]:
    out: list[dict] = []
    for ev in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", _unfold(ics), re.S):
        sm = re.search(r"^SUMMARY[^:]*:(.*)$", ev, re.M)
        dm = re.search(r"^(DTSTART[^:]*):(\S+)", ev, re.M)
        if not sm or not dm:
            continue
        title = sm.group(1).replace("\\,", ",").replace("\\;", ";").strip()
        for src, pat, kind in WHITELIST:
            if src == source and re.search(pat, title):
                t = _ics_time(dm.group(1), dm.group(2))
                if t is not None:
                    out.append(_event(source, kind, title, t))
                break
    return out


PARSERS = {"fed": parse_fomc, "bls": lambda t: parse_ics(t, "bls"), "bea": lambda t: parse_ics(t, "bea")}


# ---------------------------------------------------------------------------- 检查与合并
def sanity(source: str, events: list[dict], now: datetime) -> list[str]:
    """来源格式变了或数据不全时给出问题清单（宁可报错，不悄悄少事件）。"""
    issues = []
    yr = now.year
    if source == "fed":
        n = sum(1 for e in events if e["kind"] == "FOMC_DECISION" and e["time_utc"].startswith(str(yr)))
        if not 6 <= n <= 10:
            issues.append(f"fed: {yr} 年解析到 {n} 次议息（通常 8 次）")
    if source == "bls":
        for kind in ("US_CPI", "US_NFP"):
            if not any(e["kind"] == kind and e["time_utc"] >= now.strftime("%Y-%m-%d") for e in events):
                issues.append(f"bls: 未找到未来的 {kind}")
    if source == "bea":
        if not any(e["kind"] == "US_PCE" and e["time_utc"] >= now.strftime("%Y-%m-%d") for e in events):
            issues.append("bea: 未找到未来的 US_PCE")
    for e in events:                                       # 美国数据发布时间应在美东 8:00–14:30
        local = datetime.strptime(e["time_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).astimezone(ET)
        if not (8 <= local.hour <= 14):
            issues.append(f"{source}: {e['title']} 的美东时间 {local:%H:%M} 不在 8:00–14:30")
            break
    return issues


def diff(old: list[dict], new: list[dict]) -> list[dict]:
    """按 (来源, 类别, 标题) 比较：新增、删除、改期。"""
    key = lambda e: (e["source"], e["kind"], e["title"])          # noqa: E731
    o, n = {key(e): e for e in old}, {key(e): e for e in new}
    ch = [{"type": "added", **n[k]} for k in n.keys() - o.keys()]
    ch += [{"type": "removed", **o[k]} for k in o.keys() - n.keys()]
    ch += [{"type": "moved", **n[k], "old_time_utc": o[k]["time_utc"]}
           for k in n.keys() & o.keys() if n[k]["time_utc"] != o[k]["time_utc"]]
    return sorted(ch, key=lambda c: c["time_utc"])


def build(texts: dict[str, str | None], previous: dict | None, now: datetime, horizon_days: int | None,
          errors: dict[str, str] | None = None) -> dict:
    """texts: 来源 → 原文（None 表示抓取失败）。失败或检查不通过的来源沿用上一版事件。"""
    errors = dict(errors or {})
    prev_events = (previous or {}).get("events", [])
    lo = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ") if horizon_days else "0000"
    hi = (now + timedelta(days=horizon_days)).strftime("%Y-%m-%dT%H:%M:%SZ") if horizon_days else "9999"
    events, status = [], {}
    for src in SOURCES:
        txt = texts.get(src)
        evs, issues = [], []
        if txt is not None:
            try:
                evs = PARSERS[src](txt)
                issues = sanity(src, evs, now)
            except Exception as e:
                issues = [f"{src}: 解析失败 {type(e).__name__}"]
        else:
            issues = [f"{src}: 抓取失败 {errors.get(src, '')}".strip()]
        ok = not issues
        if not ok:                                          # 沿用上一版该来源的事件
            evs = [e for e in prev_events if e["source"] == src]
        status[src] = {"ok": ok, "issues": issues, "count": len(evs), "stale": not ok}
        events += [e for e in evs if lo <= e["time_utc"] <= hi]
    events = sorted({e["id"]: e for e in events}.values(), key=lambda e: e["time_utc"])
    prev_in_window = [e for e in prev_events if lo <= e["time_utc"] <= hi]
    return {"generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "horizon_days": horizon_days,
            "sources": status, "ok": all(s["ok"] for s in status.values()),
            "events": events, "changes": diff(prev_in_window, events) if previous else []}


def write_atomic(path: str, data: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    os.chmod(tmp, 0o640)
    tmp.replace(p)


# ---------------------------------------------------------------------------- 交易程序读取
_cache: dict[str, tuple[float, list[str]]] = {}


def load_event_times(path: str | None, max_age_days: float = 3.0) -> list[str]:
    """返回事件时间（UTC ISO 字符串）列表；按文件修改时间缓存。文件缺失或过旧时返回空列表，
    由调用方决定是否告警（文件超过 max_age_days 未更新会写入 _cache 的状态供监控读取）。"""
    if not path or not os.path.exists(path):
        return []
    mt = os.path.getmtime(path)
    c = _cache.get(path)
    if c and c[0] == mt:
        return c[1]
    try:
        data = json.loads(Path(path).read_text("utf-8"))
        times = [e["time_utc"] for e in data.get("events", [])]
    except (ValueError, KeyError, OSError):
        times = []
    _cache[path] = (mt, times)
    return times


_check_cache: dict[str, tuple[float, dict]] = {}


def calendar_check(path: str, now=None, max_age_days: float = 3.0) -> tuple[bool, str, list]:
    """交易程序用：返回 (可用, 原因, 事件时间列表[pd.Timestamp UTC])。以下任一情况不可用：
    文件不存在、损坏或时间格式非法、超过 max_age_days 未更新、所有来源失败、
    某个失败来源没有沿用到上一版事件（count = 0）、覆盖区间（generated_at + horizon_days）已不含 now。
    有来源失败但沿用了上一版事件时仍可用（原因里写明，由调用方告警）。"""
    import pandas as pd

    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
    if not path or not os.path.exists(path):
        return False, "事件文件不存在", []
    mt = os.path.getmtime(path)
    c = _check_cache.get(path)
    if c and c[0] == mt:
        info = c[1]
    else:
        info = {"error": None, "times": []}
        try:
            data = json.loads(Path(path).read_text("utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("events"), list):
                raise ValueError("缺少 events 列表")
            ts = [pd.Timestamp(e["time_utc"]) for e in data["events"]]
            info["times"] = [t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC") for t in ts]
            info["ok"], info["sources"] = data.get("ok"), data.get("sources") or {}
            g = data.get("generated_at")
            info["generated_at"] = pd.Timestamp(g).tz_convert("UTC") if g and pd.Timestamp(g).tzinfo else (
                pd.Timestamp(g).tz_localize("UTC") if g else None)
            info["horizon_days"] = data.get("horizon_days")
        except (OSError, ValueError, KeyError, TypeError) as e:           # pandas 的日期解析错误是 ValueError 子类
            info = {"error": f"事件文件无效：{type(e).__name__}", "times": []}
        _check_cache[path] = (mt, info)
    if info["error"]:
        return False, info["error"], []
    if time.time() - mt > max_age_days * 86400:
        return False, "事件文件过期", info["times"]
    reason = ""
    if info.get("ok") is not True:
        srcs = info.get("sources") or {}
        failed = [k for k, v in srcs.items() if not v.get("ok")]
        if not srcs or len(failed) == len(srcs) or any(int(srcs[k].get("count") or 0) == 0 for k in failed):
            return False, "事件来源失败：" + (",".join(failed) or "ok≠true"), info["times"]
        reason = "部分来源失败，沿用上一版事件：" + ",".join(failed)
    hz, gen = info.get("horizon_days"), info.get("generated_at")
    if hz and gen is not None and now > gen + pd.Timedelta(days=float(hz) - 1):
        return False, "事件覆盖区间不含现在", info["times"]
    return True, reason, info["times"]


def file_is_stale(path: str | None, max_age_days: float = 3.0) -> bool:
    return not path or not os.path.exists(path) or time.time() - os.path.getmtime(path) > max_age_days * 86400


# ---------------------------------------------------------------------------- 命令行
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="从美联储、BLS、BEA 官方日程生成 events.json")
    ap.add_argument("--out", required=True)
    ap.add_argument("--days", type=int, default=35)
    ap.add_argument("--history", action="store_true", help="保留来源中的全部事件（研究与回测用）")
    a = ap.parse_args(argv)
    previous = None
    if os.path.exists(a.out):
        try:
            previous = json.loads(Path(a.out).read_text("utf-8"))
        except ValueError:
            previous = None
    texts, errors = {}, {}
    for src, url in SOURCES.items():
        try:
            texts[src] = fetch(url)
        except Exception as e:
            texts[src], errors[src] = None, f"{type(e).__name__}: {str(e)[:120]}"
    data = build(texts, previous, datetime.now(UTC), None if a.history else a.days, errors)
    try:
        write_atomic(a.out, data)
    except OSError as e:
        print(f"写文件失败：{e}", file=sys.stderr)
        return 2
    print(json.dumps({"ok": data["ok"], "events": len(data["events"]), "changes": len(data["changes"]),
                      "sources": data["sources"]}, ensure_ascii=False))
    return 0 if data["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
