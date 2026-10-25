"""Weather forecasts from a bounded public search, never from model memory."""

import logging
import re
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from travel_assistant.core.config import settings
from travel_assistant.services.provider_usage import record as record_provider_usage
from travel_assistant.services.location_labels import city_zh
from travel_assistant.services.recommendation_geography import country_for_fixture_city


logger = logging.getLogger(__name__)
_URL = "https://cloud-iqs.aliyuncs.com/search/unified"
_WEATHER = re.compile(r"暴雨|大雨|中雨|小雨|阵雨|雷阵雨|雨夹雪|大雪|中雪|小雪|多云|晴|阴|雾")
_UNSAFE = re.compile(r"忽略|系统提示|开发者指令|密钥|付款|下单|ignore.{0,20}prompt|api.key|password", re.I)
_PUBLISHED = re.compile(r"(?:发布时间|发布于)\s*[:：]?\s*(?:(20\d{2})[/.-])?(\d{1,2})[/.-](\d{1,2})\s+(\d{1,2}):(\d{2})")
_DAY = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})\s*(?:周[一二三四五六日天])?")
_NMC_PAGES = {
    "Shanghai": "https://www.nmc.cn/publish/forecast/ASH/shanghai.html",
    "Beijing": "https://www.nmc.cn/publish/forecast/ABJ/beijing.html",
    "Guangzhou": "https://www.nmc.cn/publish/forecast/AGD/guangzhou.html",
    "Chengdu": "https://www.nmc.cn/publish/forecast/ASC/chengdu.html",
    "Kunming": "https://www.nmc.cn/publish/forecast/AYN/kunming.html",
}


class WeatherSearchError(Exception):
    pass


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, _attrs):
        if tag in ("script", "style"):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1

    def handle_data(self, value):
        if not self.hidden:
            self.parts.append(value)


def _published(text: str, year: int, now: datetime) -> datetime | None:
    match = _PUBLISHED.search(text[:1200])
    if not match:
        return None
    try:
        stamp = datetime(int(match[1] or year), int(match[2]), int(match[3]),
                         int(match[4]), int(match[5]), tzinfo=ZoneInfo("Asia/Shanghai"))
    except ValueError:
        return None
    utc = stamp.astimezone(timezone.utc)
    return utc if now - timedelta(hours=30) <= utc <= now + timedelta(minutes=20) else None


def _forecast(item: dict, city: str, target: date, now: datetime) -> dict | None:
    url = item.get("link")
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ("nmc.cn", "www.nmc.cn"):
        return None
    title = item.get("title")
    body = item.get("mainText")
    if not isinstance(title, str) or not isinstance(body, str):
        return None
    name = city_zh(city)
    if name not in title or name not in body[:250]:
        return None
    body = " ".join(body[:12000].split())
    if _UNSAFE.search(body):
        return None
    published = _published(body, now.astimezone(ZoneInfo("Asia/Shanghai")).year, now)
    if published is None:
        return None
    spans = list(_DAY.finditer(body))
    for index, match in enumerate(spans):
        if (int(match[1]), int(match[2])) != (target.month, target.day):
            continue
        end = spans[index + 1].start() if index + 1 < len(spans) else min(len(body), match.end() + 180)
        segment = body[match.end():end][:180]
        conditions = _WEATHER.findall(segment)
        temps = [int(value) for value in re.findall(r"(?<!\d)(-?\d{1,2})\s*℃", segment)]
        if not conditions or len(temps) < 2 or not all(-60 <= value <= 60 for value in temps[:2]):
            continue
        return {"condition": conditions[0], "low": min(temps[:2]),
                "high": max(temps[:2]), "url": url,
                "published_at": published.isoformat(), "retrieved_at": now.isoformat()}
    return None


def _official_page(item: dict, city: str, *, transport=None) -> dict | None:
    """Refresh an exact official result so stale search excerpts are not presented as current."""
    url = item.get("link")
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    try:
        allowed = (parsed.scheme == "https" and parsed.hostname == "www.nmc.cn"
                   and parsed.path.startswith("/publish/forecast/")
                   and not parsed.username and not parsed.password and parsed.port is None)
    except ValueError:
        return None
    if not allowed:
        return None
    try:
        with httpx.Client(timeout=httpx.Timeout(8.0, connect=3.0),
                          transport=transport, follow_redirects=False) as client:
            with client.stream("GET", url) as response:
                if (response.status_code != 200 or
                        "text/html" not in response.headers.get("content-type", "")):
                    return None
                payload = bytearray()
                for chunk in response.iter_bytes():
                    payload.extend(chunk)
                    if len(payload) > 256_000:
                        return None
    except httpx.HTTPError:
        return None
    parser = _VisibleText()
    parser.feed(bytes(payload).decode("utf-8", errors="replace"))
    text = " ".join(" ".join(parser.parts).split())
    position = text.find("发布时间")
    if position < 0:
        return None
    return {"link": url, "title": item.get("title", ""),
            "mainText": f"{text[:400]} {text[position:position + 3500]}"}


def search_weather(city: str, target: date, *, transport=None,
                   now: datetime | None = None) -> dict | None:
    """One Aliyun request with public city/date only; reject uncertain evidence."""
    key = settings.ALIYUN_IQS_API_KEY.strip()
    if not settings.ALIYUN_IQS_ENABLED or not key:
        raise WeatherSearchError("unconfigured")
    if country_for_fixture_city(city) != {"code": "CN", "name": "China"}:
        raise WeatherSearchError("coverage")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise WeatherSearchError("invalid")
    now = now.astimezone(timezone.utc)
    local_today = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
    if not local_today <= target <= local_today + timedelta(days=7):
        raise WeatherSearchError("date")
    body = {"query": f"{city_zh(city)} {target.year}年{target.month}月{target.day}日 天气预报 国家气象中心",
            "engineType": "GlobalBasic",
            "contents": {"mainText": True, "markdownText": False,
                         "summary": False, "rerankScore": True},
            "advancedParams": {"numResults": 5}}
    try:
        with httpx.Client(timeout=httpx.Timeout(8.0, connect=3.0),
                          transport=transport, follow_redirects=False) as client:
            with client.stream("POST", _URL,
                               headers={"Authorization": f"Bearer {key}"},
                               json=body) as response:
                if response.status_code in (401, 403):
                    record_provider_usage("search", "authentication")
                    raise WeatherSearchError("authentication")
                if response.status_code == 402:
                    record_provider_usage("search", "balance")
                    raise WeatherSearchError("balance")
                if response.status_code == 429:
                    record_provider_usage("search", "limited")
                    raise WeatherSearchError("limited")
                if response.status_code != 200:
                    record_provider_usage("search", "upstream")
                    raise WeatherSearchError("upstream")
                payload = bytearray()
                for chunk in response.iter_bytes():
                    payload.extend(chunk)
                    if len(payload) > 256_000:
                        record_provider_usage("search", "upstream")
                        raise WeatherSearchError("invalid")
                data = httpx.Response(200, content=bytes(payload)).json()
    except httpx.TimeoutException as exc:
        record_provider_usage("search", "timeout")
        raise WeatherSearchError("timeout") from exc
    except (httpx.HTTPError, ValueError) as exc:
        record_provider_usage("search", "upstream")
        raise WeatherSearchError("upstream") from exc
    record_provider_usage("search", "success")
    if not isinstance(data, dict) or not isinstance(data.get("pageItems"), list):
        raise WeatherSearchError("invalid")
    context = data.get("queryContext")
    if isinstance(context, dict) and context.get("engineType") not in (None, "GlobalBasic"):
        raise WeatherSearchError("invalid")
    for item in data["pageItems"][:5]:
        if isinstance(item, dict):
            refreshed = _official_page(item, city, transport=transport)
            # Search snippets are untrusted. Only parse the freshly fetched official page.
            forecast = _forecast(refreshed, city, target, now) if refreshed else None
            if forecast:
                logger.info("weather_search status=verified")
                return forecast
    if city in _NMC_PAGES:
        official = _official_page({"link": _NMC_PAGES[city],
                                   "title": f"{city_zh(city)}天气预报"}, city,
                                  transport=transport)
        forecast = _forecast(official, city, target, now) if official else None
        if forecast:
            logger.info("weather_search status=verified_official_fallback")
            return forecast
    logger.info("weather_search status=unverified")
    return None
