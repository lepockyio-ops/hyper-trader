"""BlockBeats（律动）新闻接入：存档 + 按币种的重大事件守卫。只依赖标准库。

判断（2026-10-29，见 docs/RESEARCH_LAB.md 的“BlockBeats 接入判断”）：
- 用途 1，事件守卫：识别跟持仓币或交易所相关的重大事件（被盗、下架、执法、暂停提现、脱锚、宕机、大额解锁），
  暂停该币新开仓一段时间。**只能收紧，不能放宽**；文件过期时框架忽略它（并告警），官方宏观日历守卫照常生效。
- 用途 2，存档：从今天起保存快讯（记录我们收到的时间）和市场数据，几个月后再交给筛因子和统计检验。
- 不做：用新闻情绪调整仓位、杠杆、止损或入场。没有历史数据能证明它有用；新闻发出时价格多半已反应。

为什么不直接用官方库：
- blockbeats-MCP（官方，MIT，1.0.4）给大模型对话用，返回整段 JSON 文本，不记录收到时间，也没有存档；
  而且 BASE_URL 写的是 http://（2026-10-29 实测：服务器在 HTTP 上直接返回 200、不跳转 HTTPS），
  API Key 会明文传输。研究 agent 用它时，安装脚本会改成 https:// 并校验。
- blockbeats-skill 让 agent 自己执行 curl（等于给 agent 一条往外发数据的通道），
  还内置了未经验证的买卖规则（如“ETF 单日净流入 > 5 亿美元 → 强烈买入信号”），不符合本框架“未经检验不进策略”的原则。
  只把它当作接口参考。

本模块直接调用官方 Pro API（https://api-pro.theblockbeats.info，接口与字段按官方文档 apiDoc）。
API Key 只从环境变量 BLOCKBEATS_API_KEY 读取（服务器上放在 root 0600 的 /etc/s03/blockbeats.env），从不写入日志或输出。

关键词规则与封禁时长全部是“示例值”，未经数据验证。
"""
from __future__ import annotations

import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

BASE_URL = "https://api-pro.theblockbeats.info"
CN_TZ = timezone(timedelta(hours=8))
STATUS_TEXT = {100: "缺少 API Key", 101: "API Key 无效", 102: "API Key 已过期", 103: "请求方法错误", -1: "通用错误"}

# 市场数据接口：每天存一次快照（研究用）
DATA_ENDPOINTS: dict[str, dict[str, str]] = {
    "btc_etf": {}, "ibit_fbtc": {}, "stablecoin_marketcap": {}, "compliant_total": {}, "daily_tx": {},
    "us10y": {"type": "1D"}, "dxy": {"type": "1D"}, "contract": {"dataType": "1D"},
    "bitfinex_long": {"symbol": "btc", "type": "1D"}, "bottom_top_indicator": {},
}


class BlockBeatsError(RuntimeError):
    pass


class _Strip(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, d):
        self.parts.append(d)


def strip_html(s: str | None) -> str:
    p = _Strip()
    p.feed(s or "")
    return re.sub(r"\s+", " ", html.unescape("".join(p.parts))).strip()


def parse_time(raw: Any) -> datetime | None:
    """create_time 有两种写法：Unix 秒（字符串）或 'Y-m-d H:i:s'。后者按北京时间解析（模型推断：官方 RSS 用 +0800）。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if s.isdigit():
        return datetime.fromtimestamp(int(s), tz=timezone.utc)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=CN_TZ).astimezone(timezone.utc)
        except ValueError:
            pass
    return None


# ======================================================================== 客户端
class BlockBeats:
    def __init__(self, api_key: str | None = None, base: str | None = None, timeout: float = 20.0):
        self._key = api_key if api_key is not None else os.environ.get("BLOCKBEATS_API_KEY", "")
        self.base = (base or os.environ.get("BLOCKBEATS_BASE_URL") or BASE_URL).rstrip("/")
        u = urllib.parse.urlparse(self.base)
        if u.scheme != "https" and u.hostname not in ("127.0.0.1", "localhost"):
            raise BlockBeatsError("只允许 HTTPS（API Key 不能明文传输）")
        self.timeout = timeout

    def _redact(self, s: str) -> str:
        return s.replace(self._key, "***") if self._key else s

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self._key:
            raise BlockBeatsError("未设置 BLOCKBEATS_API_KEY（在 https://www.theblockbeats.info/apiDoc 申请，放进服务器上的 /etc/s03/blockbeats.env）")
        url = f"{self.base}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        req = urllib.request.Request(url, headers={"api-key": self._key, "User-Agent": "s03-research/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise BlockBeatsError(f"HTTP {e.code}（{path}）") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise BlockBeatsError(self._redact(f"网络错误（{path}）：{e}")) from None
        try:
            js = json.loads(body)
        except ValueError:
            raise BlockBeatsError(f"返回的不是 JSON（{path}）") from None
        st = js.get("status")
        if st != 0:
            raise BlockBeatsError(f"status {st}：{STATUS_TEXT.get(st, '')} {self._redact(str(js.get('message', '')))}".strip())
        return js.get("data")

    name = "blockbeats"

    def newsflash(self, category: str = "all", size: int = 50, page: int = 1, lang: str = "cn") -> list[dict]:
        """size 最大 50（2026-10-29 实测：传 100 也只返回 50）；历史可翻到 2020-01（约 3360 页）。"""
        path = "/v1/newsflash" if category == "all" else f"/v1/newsflash/{category}"
        data = self.get(path, {"page": page, "size": size, "lang": lang})
        items = data.get("data") if isinstance(data, dict) else data
        return list(items or [])

    def latest(self, now: datetime, lang: str | None = None) -> list[dict]:
        out = []
        for cat in ("all", "important"):
            out += [normalize(i, cat, now) for i in self.newsflash(cat, 50, 1, lang or "cn")]
        return out

    def history_page(self, page: int) -> list[dict]:
        rows = []
        for i in self.newsflash("all", 50, page):
            r = normalize(i, "all", datetime.now(timezone.utc))
            r["received_at"], r["backfill"] = r["create_time_utc"] or r["received_at"], True
            rows.append(r)
        return rows


# ======================================================================== 规则（示例值）
def _tick(t: str) -> str:            # 大写代码：前后不能是字母数字（中文旁边也能识别）
    return rf"(?<![A-Za-z0-9]){t}(?![A-Za-z0-9])"


COINS: dict[str, list[str]] = {
    "BTC": [_tick("BTC"), r"(?i)bitcoin", "比特币"],
    "ETH": [_tick("ETH"), r"(?i)(?<![a-z])ethereum", "以太坊"],
    "SOL": [_tick("SOL"), r"(?i)solana"],
    "BNB": [_tick("BNB"), r"(?i)bnb chain", "币安币"],
    "HYPE": [_tick("HYPE"), r"(?i)hyperliquid"],
}
VENUES: dict[str, list[str]] = {
    "binance": [r"(?i)binance", "(?<![特代定货])币安"],   # “比特币安全”“代币安全港”里的“币安”不算
    "hyperliquid": [r"(?i)hyperliquid"],
    "okx": [_tick("OKX"), "欧易"],
}
# 类别 → (正则, 封禁小时数, 币种命中方式, 是否适用于交易所级事件)
#   币种命中方式："chain" = 币种必须是主语（“以太坊主网 …”“Solana 网络 …”“Solana：…” 官方声明），
#                 第三方协议被盗时顺带提到 ETH 不算；"plain" = 币种与风险词相距 8 个字以内（“下架 SOL”“HYPE 大额解锁”）；
#                 "" = 不按币种封禁（只看交易所）。
CATEGORIES: dict[str, tuple[str, int, str, bool]] = {
    "security": (r"(?i)hack(ed|er)?s?\b|exploit(ed)?\b|drain(ed)?\b|stolen|被盗|黑客|遭(到)?攻击|攻击事件|安全事件|漏洞(被)?利用|漏洞攻击",
                 24, "chain", True),
    "delisting": (r"(?i)delist|下架|退市", 72, "plain", False),
    "enforcement": (r"(?i)\bSEC\b.{0,40}(sue|charge|lawsuit|enforcement)|lawsuit|indict|起诉|指控|执法行动|立案调查|冻结资产|认罪",
                    12, "", True),
    "withdrawal_halt": (r"(?i)(suspend|halt|pause)\w*\s+(\w+\s+){0,3}withdraw|暂停.{0,10}(提币|提现|充提|提款)|(提币|提现)(暂停|中断)",
                        12, "plain", True),
    "depeg": (r"(?i)de-?peg|脱锚", 6, "plain", False),
    "outage": (r"(?i)outage|chain halt|network halt|halted trading|暂停交易|宕机|停止出块|出块(停止|中断)|暂停出块|集群停止|网络中断|系统.{0,4}故障|API.{0,6}(故障|中断)|中断|区块.{0,6}停止|性能下降",
               6, "chain", True),
    "unlock": (r"(?i)token unlock|大额解锁|代币解锁", 6, "plain", False),
}
# 某类别下按币种封禁时额外排除：交易对 / 杠杆对下架不等于这个币被下架
COIN_EXCLUDE = {"delisting": r"(?i)交易对|杠杆|保证金|借贷|理财|pair|margin",
                # 个人钱包被盗 / 钓鱼 ≠ 公链本身出事
                "security": r"私钥|助记词|钓鱼|某用户|一名用户|受害者|某地址|恶意授权|签名"}
# 排除（示例值；2026-10-29 用两家一周快讯 + 6 个历史事件人工复核后加入）
#   EXCLUDE_DOC：整条快讯里出现就不算（事件已解决、否认 / 未受影响、悬赏审计、刑事判决、演练测试）
#   EXCLUDE_SENT：只排除出现它的那一句（例行升级维护、拒绝支付赎金）
EXCLUDE_DOC = (r"(?i)bounty|赏金|审计|audit|recovered|追回|已修复|恢复(提币|提现|交易|出块|正常)|已恢复|重新出块|已重新|resum(ed|es)"
               r"|成功(阻止|拦截)|未造成|未受|未遭|不受影响|没有发生|并非|否认|澄清|被判|判处|监禁|刑期|赔付|辟谣|谣言|演练|测试网|testnet")
EXCLUDE_SENT = (r"(?i)升级|维护|硬分叉|maintenance|upgrade|拒绝支付"
                r"|若|如果|假如|有朝一日|建议|提醒|谨防|防范"                    # 假设与提醒，不是已发生的事件
                r"|黑客松|黑客马拉松|hackathon|Hack\s*AI"                         # 黑客松是活动，不是攻击
                r"|^\s*(此前|据此前|此前消息|去年|今年\s*\d+\s*月)|昨日报道")   # 背景回顾（旧闻重述）
MAINTENANCE = r"(?i)升级|维护|硬分叉|maintenance|upgrade"     # 整条提到升级维护时，不判暂停提现（宕机照判：真实宕机报道常提到升级重启）
EXCLUDE = EXCLUDE_DOC + "|" + EXCLUDE_SENT[4:]          # 兼容旧名
# 汇总帖：正文是“1. … 2. …”编号列表（星球早 / 午 / 晚讯、日报等）
DIGEST_BODY = r"^\s*1\s*[\.、．]"
DIGEST_BODY2 = r"[；;。\s]\s*2\s*[\.、．]"
# 媒体自带的开头：“BlockBeats 消息，10 月 29 日，”“Odaily星球日报讯”
SOURCE_PREFIX = (r"^\s*(律动\s*)?(BlockBeats|动察\s*Beating\s*AI)\s*(消息|快讯)\s*[，,:：]?\s*|^\s*Odaily\s*星球日报讯\s*[，,:：]?\s*")
DATE_PREFIX = r"^\s*\d{1,2}\s*月\s*\d{1,2}\s*日\s*[，,]?\s*"
# 资金流向交易所 / 在交易所上交易 ≠ 交易所本身出事（只对交易所级的被盗类判断生效）
VENUE_FLOW = r"存入|转入|充值|充币|转移至|转至|提出|提取|流入|流出|还款|划转"
VENUE_LOCATION = r"(在|于|通过)\s*{v}\s*(上|平台|以|交易|做多|做空|开|建|买|卖)"
CHAIN_WORD = r"\s*(?i:主网|网络|链(?!上)|区块链|公链|客户端|共识|验证者|节点|mainnet|network|chain|blockchain|validators?|core)(?!资产)"
COIN_LOCATION = r"(在|于)\s*{c}[^，,]{{0,20}}?(上|中)"          # “在 BNB Chain 上铸造”：链只是发生地点，不是出事的主体
_QUOTE = r"(?<=[A-Za-z0-9])/(BTC|ETH|BNB|SOL|USDT|USDC|FDUSD|USD1|TRY|EUR)(?![A-Za-z0-9])"   # 交易对的计价币不算主语
_QTY = r"\d[\d,.]*\s*(万|亿|千)?\s*(枚|个)?\s*(BTC|ETH|SOL|BNB|HYPE|比特币|以太坊)"            # “8.5 枚 BTC”是金额，不是主语
_TICKER = r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{1,9}(?![A-Za-z0-9])"
_NOT_TOKEN = {"OKX", "HTX", "API", "SEC", "CEO", "CFTC", "DOJ", "USD", "US", "AI", "ETF"}
_SENT_SPLIT = r"[。；;！!？?\n]+"


def _scoped(p: str) -> str:            # 把开头的 (?i) 变成只作用于这一段的 (?i:…)，才能和后面的模式拼接
    return f"(?i:{p[4:]})" if p.startswith("(?i)") else f"(?:{p})"


@dataclass(frozen=True)
class Hit:
    target: str          # 币种代码，或 "@venue"
    category: str
    hours: int


def body_text(content: str) -> str:
    """规则用的正文：去掉 HTML、媒体自带的“BlockBeats 消息，10 月 29 日，”开头。"""
    t = strip_html(content) if "<" in (content or "") else (content or "")
    t = re.sub(SOURCE_PREFIX, "", t)
    return re.sub(DATE_PREFIX, "", t).strip()


def is_digest(body: str) -> bool:
    return bool(re.search(DIGEST_BODY, body) and re.search(DIGEST_BODY2, body))


def classify(content: str) -> list[Hit]:
    """可复现的关键词规则，**只看正文，不看标题**（v2.5，按你的要求）。

    做法：去掉媒体开头 → 汇总帖（编号列表）整条跳过 → 整条里有“已恢复 / 否认 / 未受影响 …”就跳过 →
    按句子（。；！？换行）逐句判断，任一句命中即命中。同一句里必须同时出现“币种或交易所”和“风险词”。
    逐句规则：
    - 被盗 / 宕机按币种封禁时，币种必须是主语（币种后 12 个字内跟着“主网 / 网络 / 验证者 …”，或句子以“Solana：”开头）；
      第三方协议被盗时“损失 8.5 枚 ETH”不算；
    - 下架 / 解锁 / 脱锚要求币种紧挨着风险词，交易对 / 杠杆对下架不算；
    - 交易所级事件：交易所名与风险词在同一句；资金“存入 / 转入”交易所、“在 Hyperliquid 上交易”的被盗类句子不算
    - 假设 / 提醒句（若、如果、建议、谨防…）和背景回顾句（此前、去年…）不算；整条提到升级维护时不判暂停提现；
      交易所暂停“某个别币”的充提（句中有其他代币代码）不封整个交易所；Hyperliquid 平台事件同时封 HYPE。
    规则是在 1 周快讯和 6 个历史事件上人工调的（有过拟合风险），上线前用 `news backfill` + `news replay` 在别的时间段复核。
    """
    body = body_text(content)
    if not body or is_digest(body) or re.search(EXCLUDE_DOC, body):
        return []
    maint = bool(re.search(MAINTENANCE, body))
    hits: set[Hit] = set()
    for sent in re.split(_SENT_SPLIT, body):
        if sent.strip():
            hits |= _classify_sentence(sent, maint)
    return sorted(hits, key=lambda x: (x.target, x.category))


def classify_item(item: dict) -> list[Hit]:
    """存档里的一条快讯 → 命中。只用正文（content）；正文为空的快讯不参与判断。"""
    return classify(item.get("content", ""))


def _classify_sentence(sent: str, maint: bool = False) -> set[Hit]:
    if re.search(EXCLUDE_SENT, sent):
        return set()
    text = re.sub(_QTY, " ", re.sub(_QUOTE, "", sent))
    cats = [(c, pat, h, mode, venue_ok) for c, (pat, h, mode, venue_ok) in CATEGORIES.items()
            if re.search(pat, text) and not (maint and c == "withdrawal_halt")]
    if not cats:
        return set()
    ours = set(COINS)
    hits: set[Hit] = set()
    for coin, pats in COINS.items():
        names = [p for p in pats if "hyperliquid" not in p]          # “Hyperliquid 下架 JELLY”不是 HYPE 被下架
        for c, pat, h, mode, _ in cats:
            if mode == "chain":
                ok = any(re.search(_scoped(p) + r"[^，,：:]{0,12}?" + CHAIN_WORD, text) or re.match(r"\s*" + _scoped(p) + r"\s*[：:]", text)
                         for p in pats)
                if ok and c == "security" and (re.search(COIN_EXCLUDE["security"], text)
                                               or any(re.search(COIN_LOCATION.format(c=_scoped(p)), text) for p in pats)):
                    ok = False
            elif mode == "plain":
                ok = any(re.search(f"{_scoped(p)}.{{0,8}}{_scoped(pat)}|{_scoped(pat)}.{{0,8}}{_scoped(p)}", text) for p in names)
                ok = ok and not (c == "delisting" and re.search(COIN_EXCLUDE[c], text))
            else:
                ok = False
            if ok:
                hits.add(Hit(coin, c, h))
    other_tokens = set(re.findall(_TICKER, text)) - ours - _NOT_TOKEN
    for venue, pats in VENUES.items():
        if not any(re.search(p, text) for p in pats):
            continue
        located = any(re.search(VENUE_LOCATION.format(v=_scoped(p)), text) for p in pats)
        for c, _, h, _, venue_ok in cats:
            if not venue_ok or (c == "withdrawal_halt" and other_tokens):
                continue
            if c == "security" and (located or re.search(VENUE_FLOW, text)):
                continue
            hits.add(Hit(f"@{venue}", c, h))
            if venue == "hyperliquid":                  # 平台出事直接影响 HYPE
                hits.add(Hit("HYPE", c, h))
    return hits


# ======================================================================== 存档
def normalize(item: dict, category: str, received_at: datetime, source: str = "blockbeats") -> dict:
    t = parse_time(item.get("create_time"))
    return {"source": source, "id": item.get("id"), "category": category, "title": strip_html(item.get("title")),
            "content": strip_html(item.get("content"))[:2000], "link": item.get("link") or item.get("url") or "",
            "create_time_raw": str(item.get("create_time", "")), "create_time_utc": t.isoformat() if t else None,
            "received_at": received_at.isoformat()}


class Archive:
    """archive/news/YYYY-MM.jsonl（只追加）+ archive/news/recent.json（近 7 天，用于守卫与去重）+ archive/data/日期.json。
    历史回填写到 archive/news/backfill-<来源>.jsonl（received_at = 发布时间，backfill = true）。
    去重键：（来源, id, 分类）；v2.3 早期的行没有 source 字段，按 blockbeats 处理。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        (self.root / "news").mkdir(parents=True, exist_ok=True)
        (self.root / "data").mkdir(parents=True, exist_ok=True)

    @property
    def recent_path(self) -> Path:
        return self.root / "news" / "recent.json"

    def recent(self) -> list[dict]:
        try:
            return json.loads(self.recent_path.read_text("utf-8"))
        except (OSError, ValueError):
            return []

    def add(self, items: list[dict], now: datetime) -> int:
        recent = self.recent()
        seen = {_key(r) for r in recent if r.get("id") is not None}
        new = [i for i in items if i.get("id") is not None and _key(i) not in seen]
        new = list({_key(i): i for i in new}.values())
        if new:                                              # 每个（id, 分类）存一行：同一条快讯可同时属于“全部”和“重要”
            month = self.root / "news" / f"{now:%Y-%m}.jsonl"
            with open(month, "a", encoding="utf-8") as fh:
                for i in new:
                    fh.write(json.dumps(i, ensure_ascii=False) + "\n")
        cutoff = (now - timedelta(days=7)).isoformat()
        merged = [r for r in recent + new if r["received_at"] >= cutoff]
        _write_atomic(self.recent_path, merged)
        return len(new)

    def save_data(self, snapshot: dict, now: datetime) -> Path:
        p = self.root / "data" / f"{now:%Y-%m-%d}.json"
        _write_atomic(p, snapshot)
        return p

    def iter_all(self):
        for f in sorted((self.root / "news").glob("*.jsonl")):
            for line in f.read_text("utf-8").splitlines():
                if line.strip():
                    yield json.loads(line)


def _key(r: dict) -> tuple:
    return (r.get("source", "blockbeats"), r["id"], r["category"])


def _write_atomic(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, path)


# ======================================================================== 守卫
def build_blocks(items: list[dict], now: datetime) -> list[dict]:
    """从近期快讯（可来自多个来源）算出仍然有效的封禁。起点用新闻自带时间（解析不了就用收到时间），过期的不再输出。
    同一（目标, 类别）只留截止最晚的一条，所以两家报同一件事不会叠加时长。"""
    out: dict[tuple[str, str], dict] = {}
    for i in items:
        start = datetime.fromisoformat(i.get("create_time_utc") or i["received_at"])
        for h in classify_item(i):
            until = start + timedelta(hours=h.hours)
            if until <= now:
                continue
            key = (h.target, h.category)
            if key not in out or until.isoformat() > out[key]["until_utc"]:
                out[key] = {"target": h.target, "category": h.category, "until_utc": until.isoformat(),
                            "source": i.get("source", "blockbeats"), "news_id": i.get("id"), "title": i.get("title", "")[:200],
                            "excerpt": body_text(i.get("content", ""))[:200], "link": i.get("link", ""),
                            "received_at": i["received_at"]}
    return sorted(out.values(), key=lambda b: (b["target"], b["category"]))


def write_guard(path: str | Path, blocks: list[dict], now: datetime, ok: bool, error: str | None = None,
                sources: dict | None = None) -> None:
    data = {"generated_at": now.isoformat(), "ok": ok, "error": error, "sources": sources or {},
            "rules": "fwresearch/blockbeats.py classify v2.5（只看正文；关键词规则与时长均为示例值）", "blocks": blocks}
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(p, data)


def load_blocks(path: str | None, now: datetime | None = None, max_age_min: float = 30.0) -> tuple[list[dict], str]:
    """框架用：返回 (仍然有效的封禁, 状态)。状态：disabled / ok / degraded / stale / error。
    degraded = 文件是新的，但两个来源中有一个这一轮失败了（封禁照常生效，交易程序记日志告警）。
    文件缺失 = 未启用；文件过期或读不了 = 忽略封禁（只是附加守卫，不能因为它卡住交易），由状态告警。"""
    if not path:
        return [], "disabled"
    p = Path(path)
    if not p.exists():
        return [], "disabled"
    now = now or datetime.now(timezone.utc)
    try:
        data = json.loads(p.read_text("utf-8"))
        gen = datetime.fromisoformat(data["generated_at"])
    except (OSError, ValueError, KeyError):
        return [], "error"
    if now - gen > timedelta(minutes=max_age_min):
        return [], "stale"
    live = [b for b in data.get("blocks", []) if datetime.fromisoformat(b["until_utc"]) > now]
    srcs = data.get("sources") or {}
    if any(not v.get("ok") for v in srcs.values()):
        return live, "degraded"                         # 有来源失败：剩下的来源照常封禁，但要告警
    return live, "ok"


def blocked(coin: str, venue: str, blocks: list[dict]) -> dict | None:
    """这个币（或它所在的交易所）当前有没有封禁。返回第一条命中的封禁。"""
    for b in blocks:
        if b["target"] == coin.upper() or b["target"] == f"@{venue.lower()}":
            return b
    return None


# ======================================================================== 汇总（研究用）
def daily_counts(items, coins: list[str], source: str | None = None) -> list[dict]:
    """按收到时间的 UTC 日期，统计每个币的快讯数、重要快讯数、风险命中数（长表）。
    source 只统计一个来源（两家报同一件事会重复计数，做因子时建议只用一家，例如历史最长的 odaily）。"""
    sets: dict[tuple[str, str], dict[str, set]] = {}
    for i in items:
        if source and i.get("source", "blockbeats") != source:
            continue
        day = i["received_at"][:10]
        text = f"{i.get('title', '')} {i.get('content', '')[:300]}"
        hits = {h.target for h in classify_item(i)}
        for c in coins:
            if any(re.search(p, text) for p in COINS.get(c, [_tick(c)])):
                s = sets.setdefault((day, c), {"news": set(), "important": set(), "risk": set()})
                s["news"].add(i.get("id"))
                if i.get("category") == "important":
                    s["important"].add(i.get("id"))
                if c in hits:
                    s["risk"].add(i.get("id"))
    return [{"date": d, "coin": c, "n_news": len(s["news"]), "n_important": len(s["important"]), "n_risk": len(s["risk"])}
            for (d, c), s in sorted(sets.items())]


def replay(items, since: datetime, until: datetime) -> dict:
    """守卫回放（研究 / 审规则用）：把存档里 [since, until) 的快讯按当前规则重新分类，
    统计每个目标被封的时间占比与命中标题。用来检查误报——封禁占比过高说明规则太宽。"""
    iv: dict[str, list[tuple[datetime, datetime]]] = {}
    hits: list[dict] = []
    seen: set = set()
    for i in items:
        t = i.get("create_time_utc") or i.get("received_at")
        if not t:
            continue
        start = datetime.fromisoformat(t)
        if not (since <= start < until):
            continue
        k = (i.get("source", "blockbeats"), i.get("id"))
        if k in seen:
            continue
        seen.add(k)
        for h in classify_item(i):
            iv.setdefault(h.target, []).append((start, min(start + timedelta(hours=h.hours), until)))
            hits.append({"time": t, "source": k[0], "target": h.target, "category": h.category, "title": i.get("title", "")[:120]})
    span = (until - since).total_seconds()
    pct = {}
    for tgt, v in iv.items():
        v.sort()
        tot, (cs, ce) = 0.0, v[0]
        for s0, e0 in v[1:]:
            if s0 > ce:
                tot += (ce - cs).total_seconds()
                cs, ce = s0, e0
            else:
                ce = max(ce, e0)
        tot += (ce - cs).total_seconds()
        pct[tgt] = round(100 * tot / span, 1)
    return {"since": since.isoformat(), "until": until.isoformat(), "news": len(seen), "blocked_pct": dict(sorted(pct.items())),
            "hits": sorted(hits, key=lambda x: x["time"])}


# ======================================================================== 一轮采集（可多来源）
def collect(clients, archive: Archive, guard_out: str | None, lang: str | None = None,
            now: datetime | None = None) -> dict:
    """clients：一个或多个来源（BlockBeats / fwresearch.odaily.Odaily），各自有 name 与 latest(now, lang)。
    任一来源成功，守卫文件就记 ok（两家互为备份）；失败的来源写进 error 与 sources，`news blocks` 能看到。"""
    now = now or datetime.now(timezone.utc)
    clients = clients if isinstance(clients, (list, tuple)) else [clients]
    got, errs, status = [], [], {}
    for c in clients:
        try:
            rows = c.latest(now, lang)
            got += rows
            status[c.name] = {"ok": True, "fetched": len(rows)}
        except (BlockBeatsError, OSError, ValueError, KeyError, TypeError) as e:
            msg = c._redact(str(e)) if hasattr(c, "_redact") else str(e)
            errs.append(f"{c.name}: {msg}")
            status[c.name] = {"ok": False, "error": msg}
    ok = any(v["ok"] for v in status.values())
    err = "; ".join(errs) or None
    n_new = archive.add(got, now) if got else 0
    blocks = build_blocks(archive.recent(), now)
    if guard_out:
        write_guard(guard_out, blocks, now, ok=ok, error=err, sources=status)
    return {"ok": ok, "error": err, "sources": status, "fetched": len(got), "new": n_new, "blocks": len(blocks),
            "targets": sorted({b["target"] for b in blocks})}


def backfill(client, archive: Archive, since: datetime, sleep: float = 1.0, max_pages: int | None = None,
             start_page: int = 1, log=None) -> dict:
    """研究用：从最新往回翻页，把 since 之后的快讯写进 archive/news/backfill-<来源>.jsonl（按 id 去重，可中断后续跑）。
    注意（写进报告）：回填的是“现在还在网站上的”快讯——删掉的不会出现，正文可能被改过，重要标记可能是事后补的；
    received_at 用的是发布时间，不是我们当时真正收到的时间。所以回填数据只适合做因子 / 事件研究，结论要打折扣。"""
    import time as _t
    path = archive.root / "news" / f"backfill-{client.name}.jsonl"
    state = archive.root / "news" / f"backfill-{client.name}.state.json"
    have: set = set()
    if path.exists():
        for line in path.read_text("utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                have.add((r["id"], r["category"]))
    if start_page == 1 and state.exists():
        try:
            start_page = int(json.loads(state.read_text("utf-8")).get("next_page", 1))
        except (OSError, ValueError):
            pass
    page, added, oldest = start_page, 0, None
    with open(path, "a", encoding="utf-8") as fh:
        while max_pages is None or page < start_page + max_pages:
            rows = client.history_page(page)
            if not rows:
                break
            for r in rows:
                if (r["id"], r["category"]) not in have and r.get("create_time_utc") and r["create_time_utc"] >= since.isoformat():
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                    have.add((r["id"], r["category"]))
                    added += 1
            times = [r["create_time_utc"] for r in rows if r.get("create_time_utc")]
            oldest = min(times) if times else oldest
            page += 1
            _write_atomic(state, {"next_page": page, "oldest": oldest})
            if log and page % 50 == 0:
                log(f"{client.name} page {page} oldest {oldest} added {added}")
            if oldest and oldest < since.isoformat():
                _write_atomic(state, {"next_page": 1, "oldest": oldest, "done": True})
                break
            _t.sleep(sleep)
    return {"source": client.name, "added": added, "oldest": oldest, "next_page": page, "file": str(path)}


def snapshot_data(client: BlockBeats, archive: Archive, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    snap: dict[str, Any] = {"received_at": now.isoformat(), "data": {}, "errors": {}}
    for name, params in DATA_ENDPOINTS.items():
        try:
            snap["data"][name] = client.get(f"/v1/data/{name}", params or None)
        except BlockBeatsError as e:
            snap["errors"][name] = str(e)
    p = archive.save_data(snap, now)
    return {"ok": not snap["errors"], "saved": str(p), "endpoints": len(snap["data"]), "errors": snap["errors"]}


def check(client) -> dict:
    """连通性自检：只请求第 1 页；输出里不含 Key。适用于任一来源。"""
    base = getattr(client, "base", "")
    try:
        rows = client.history_page(1)
        t = max((r["create_time_utc"] for r in rows if r.get("create_time_utc")), default=None)
        return {"ok": True, "source": client.name, "items": len(rows), "latest_create_time_utc": t, "base": base}
    except (BlockBeatsError, OSError, ValueError, KeyError) as e:
        msg = client._redact(str(e)) if hasattr(client, "_redact") else str(e)
        return {"ok": False, "source": client.name, "error": msg, "base": base}
