"""Odaily（星球日报）公开快讯接口：第二个新闻来源。只依赖标准库，不需要 API Key。

接口文档：https://github.com/ODAILY/REST-API（Base URL https://api.odaily.news，全部 GET、HTTPS、无认证）。
2026-10-29 实测（已核验事实）：
- 快讯共约 38 万条，翻页可回到 2018-05；size 可到 100；
- publishTimestamp 是毫秒 UTC，publishDate 是北京时间字符串（用前者）；
- 文档写的 isImportant=true 过滤参数实际不生效（返回总数与不过滤相同），但每条快讯自带 isImportant 字段；
- 有单独的“交易所公告”分类（exchange-announcement）；没有限频响应头，限频与使用条款未知 → 回填时默认每秒 1 次。
与 BlockBeats 同一周快讯对比：事件层面高度重合，发布时间中位差约 30 秒（Odaily 略早），见 docs/RESEARCH_LAB.md。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from fwresearch.blockbeats import BlockBeatsError, strip_html

BASE_URL = "https://api.odaily.news"


class OdailyError(BlockBeatsError):
    pass


def normalize(item: dict, category: str, received_at: datetime) -> dict:
    ts = item.get("publishTimestamp")
    t = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc) if ts else None
    return {"source": "odaily", "id": item.get("id"), "category": category, "title": strip_html(item.get("title")),
            "content": strip_html(item.get("content"))[:2000], "link": item.get("link") or item.get("sourceUrl") or "",
            "create_time_raw": str(item.get("publishDate", "")), "create_time_utc": t.isoformat() if t else None,
            "received_at": received_at.isoformat()}


class Odaily:
    name = "odaily"

    def __init__(self, base: str | None = None, timeout: float = 20.0):
        self.base = (base or BASE_URL).rstrip("/")
        u = urllib.parse.urlparse(self.base)
        if u.scheme != "https" and u.hostname not in ("127.0.0.1", "localhost"):
            raise OdailyError("只允许 HTTPS")
        self.timeout = timeout

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "s03-research/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                js = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raise OdailyError(f"HTTP {e.code}（{path}）") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise OdailyError(f"网络错误（{path}）：{e}") from None
        except ValueError:
            raise OdailyError(f"返回的不是 JSON（{path}）") from None
        if js.get("code") != 200 or not js.get("success", True):
            raise OdailyError(f"code {js.get('code')}：{js.get('msg', '')}")
        return js.get("data")

    def newsflash(self, category: str = "all", size: int = 100, page: int = 1, lang: str = "zh-cn") -> list[dict]:
        path = "/api/v1/newsflash" if category == "all" else f"/api/v1/newsflash/{category}"
        data = self.get(path, {"page": page, "size": size, "lang": lang}) or {}
        return list(data.get("list") or [])

    @staticmethod
    def _rows(items: list[dict], category: str, now: datetime) -> list[dict]:
        out = []
        for i in items:
            out.append(normalize(i, category, now))
            if i.get("isImportant"):                      # 与 BlockBeats 的“重要”分类对齐，每个（id, 分类）一行
                out.append(normalize(i, "important", now))
        return out

    def latest(self, now: datetime, lang: str | None = None) -> list[dict]:
        lang = lang if lang and lang != "cn" else "zh-cn"
        return (self._rows(self.newsflash("all", 100, 1, lang), "all", now)
                + self._rows(self.newsflash("exchange-announcement", 20, 1, lang), "exchange", now))

    def history_page(self, page: int) -> list[dict]:
        rows = self._rows(self.newsflash("all", 100, page), "all", datetime.now(timezone.utc))
        for r in rows:
            r["received_at"], r["backfill"] = r["create_time_utc"] or r["received_at"], True
        return rows
