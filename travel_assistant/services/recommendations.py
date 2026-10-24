"""Traveler-scoped, evidence-only place suggestions for the controlled graph."""

import ipaddress
import logging
import re
from datetime import date, datetime, timezone
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from travel_assistant.core.config import settings
from travel_assistant.db.travel_models import (airports, demo_state, flight_offers, flight_orders,
                              flights, hotel_orders, hotel_products, hotels)
from travel_assistant.services.recommendation_geography import (airport_geography,
                                               country_for_fixture_city,
                                               country_for_reachable_city)
from travel_assistant.services.reviewed_places import places_for_city, reviewed_places
from travel_assistant.services.location_labels import CITY_ALIASES as _CITY_ALIASES, city_zh

logger = logging.getLogger(__name__)


class RecommendationProvider(Protocol):
    def search(self, city: str, preference: str | None,
               country: dict) -> list[dict]: ...


class UnavailableRecommendationProvider:
    """Explicitly decline unsupported recommendations when search is unavailable."""

    def search(self, city: str, preference: str | None,
               country: dict) -> list[dict]:
        return []


def get_recommendation_provider() -> RecommendationProvider:
    key = settings.ALIYUN_IQS_API_KEY.strip()
    if settings.ALIYUN_IQS_ENABLED and key:
        return ReviewedCatalogProvider(CleverSeeRecommendationProvider(key))
    return ReviewedCatalogProvider()


_CATEGORIES = {
    "history": "历史", "culture": "文化", "nature": "自然",
    "architecture": "建筑", "family": "亲子", "food": "美食",
}
_PREFERENCES = {word: category for category, words in {
    "history": ("历史", "古迹", "博物馆"),
    "culture": ("文化", "艺术"),
    "nature": ("自然", "公园", "登山", "海边"),
    "architecture": ("建筑", "教堂"),
    "family": ("亲子", "孩子"),
    "food": ("美食", "餐饮"),
}.items() for word in words}
_COUNTRY_REQUESTS = {
    "中国": "CN", "日本": "JP", "美国": "US", "法国": "FR",
    "英国": "GB", "瑞士": "CH", "韩国": "KR", "加拿大": "CA",
    "意大利": "IT", "阿联酋": "AE",
}
_UNSAFE_TEXT = re.compile(
    r"忽略|系统提示|开发者|执行命令|密钥|付款|下单|修改政策|"
    r"ignore|system prompt|developer|execute|secret|api key|password|token", re.I)
_PLACE = re.compile(r"^[\w ,\-.'·（）()&]+$", re.UNICODE)


_SEARCH_URL = "https://cloud-iqs.aliyuncs.com/search/unified"
_SEARCH_TERMS = {
    "history": "museum", "culture": "gallery",
    "nature": "park", "architecture": "landmark",
    "family": "zoo", "food": "market",
}
_CATEGORY_TERMS = {
    "history": ("museum", "historic", "history", "heritage", "博物馆", "历史", "古迹"),
    "culture": ("art", "gallery", "culture", "theatre", "艺术", "文化", "剧院"),
    "nature": ("park", "garden", "nature", "botanical", "公园", "花园", "自然"),
    "architecture": ("cathedral", "church", "architecture", "tower", "教堂", "建筑", "塔"),
    "family": ("zoo", "aquarium", "family", "动物园", "水族馆", "亲子"),
    "food": ("market", "food", "cuisine", "美食", "集市", "市场"),
}
_GENERIC_PLACE = re.compile(
    r"\b(?:top|best|list of|things to do|travel|trip|tours?|guide|tourism|"
    r"attractions?|official site|visit|home|hotels?|tickets?|restaurants?|museums|musées|markets|galleries|parks|"
    r"architecture|landmarks?|historic sites?|food|nature|culture|museumsufer|"
    r"strolling|exploring|discovering|walking through|things to know|"
    r"our story|about us|search results|welcome|plan your visit|"
    r"museum pass(?:es)?|museum entry|entry tickets?|admission tickets?|"
    r"discount admission|collections|historical treasures|photo gallery|"
    r"image gallery|lcsd museum|halaman depan|homepage)\b|"
    r"景点推荐|旅游|攻略|十大|排行榜|官方网站|首页|门票|酒店|建筑之旅|美食推荐|"
    r"最佳|值得一游|掠影|中的|组图|探索|国家[A-Z]{2,}级景区", re.I)
_INSTRUCTION_TEXT = re.compile(
    r"忽略.{0,20}(?:指令|提示)|系统提示|开发者指令|密钥|下单|付款|"
    r"ignore.{0,25}(?:instruction|prompt)|system prompt|developer message|"
    r"api key|password|bearer token|place an order|book now", re.I)
_NON_VENUE = re.compile(r"\b(?:hotel|hostel|holiday inn|resort|airport hotel)\b", re.I)
_STORY_TITLE = re.compile(r"\b(?:is|are|was|were|my|our)\b|\.\.\.", re.I)
_DESCRIPTIVE_TITLE = re.compile(r"\b(?:in|near)\b.*[,]", re.I)
_AWAY_FROM_CITY = re.compile(
    r"\b(?:away from|south of|north of|east of|west of|km from)\b|"
    r"以.{0,3}[南北东西].{0,8}\d+\s*(?:公里|千米)", re.I)
_HISTORY_VENUE = re.compile(
    r"\b(?:museum|museo|musée|musee|museu|heritage|historical|historic|"
    r"archaeological|memorial|monument|palace|fort|castle|temple|church|"
    r"cathedral|gallery)\b|博物馆|历史|遗址|纪念馆", re.I)
_THEME_VENUE = {
    "culture": re.compile(r"\b(?:gallery|museum|theatre|theater|opera house|arts? center|arts? centre)\b|画廊|美术馆|博物馆|剧院", re.I),
    "nature": re.compile(r"\b(?:park|garden|botanical|nature reserve)\b|公园|花园|植物园|自然保护区", re.I),
    "architecture": re.compile(r"\b(?:cathedral|minster|church|mosque|tower|palace|temple|bridge|landmark)\b|教堂|清真寺|塔|宫殿|寺|桥", re.I),
    "family": re.compile(r"\b(?:zoo|aquarium)\b|动物园|水族馆", re.I),
    "food": re.compile(r"\bmarket\b|市场|集市", re.I),
}
_COUNTRY_ALIASES = {
    "AE": ("UAE", "阿联酋"), "CA": ("加拿大",),
    "CH": ("瑞士",), "CN": ("中国",), "FR": ("法国",),
    "GB": ("UK", "England", "Scotland", "英国"),
    "HK": ("香港",), "IT": ("Italia", "意大利"),
    "JP": ("日本",), "KR": ("韩国",), "TW": ("台湾",),
    "US": ("USA", "美国"),
}
_CONFLICTING_REGIONS = {
    "Rome": ("Georgia",), "Paris": ("Texas",),
    "London": ("Ontario",),
}
_BLOCKED_SOURCE_HOSTS = (
    "baidu.com", "baike.so.com", "quark.cn", "baike.com",
    "baike.sogou.com", "youtube.com", "youtu.be",
    "tiktok.com", "facebook.com", "instagram.com",
    "phb123.com", "aizhan.com", "zhijianshang.com",
    "autohome.com.cn",
    "sohu.com", "dict.youdao.com", "wheree.com", "world-guides.com",
    "planetware.com", "triptojapan.com", "aroundus.com",
    "bing.com", "meipian.cn", "m.ctrip.com", "sina.cn",
    "ctrip.com", "trip.com", "qunar.com", "mafengwo.cn",
    "tuniu.com", "tripadvisor.com", "wannar.com",
)
_SEARCH_COUNTRY_SHORT = {
    "AE": "UAE", "GB": "UK", "KR": "Korea", "US": "USA",
}


class RecommendationSearchError(Exception):
    """Safe error classification; never includes server text or credentials."""

    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(kind)


class CleverSeeRecommendationProvider:
    """Search public city/theme words and keep only directly supported places."""

    live_source = True

    def __init__(self, api_key: str, transport: httpx.BaseTransport | None = None,
                 diagnostics: list[dict] | None = None):
        self._api_key = api_key
        self._transport = transport
        self._diagnostics = diagnostics

    def search(self, city: str, preference: str | None,
               country: dict | None = None) -> list[dict]:
        country = country or _known_country(city)
        if (not re.fullmatch(r"[\w ,.'-]{2,80}", city)
                or preference not in (None, *_SEARCH_TERMS)
                or not _valid_country(country)):
            return []
        country_term = _SEARCH_COUNTRY_SHORT.get(country["code"], country["name"])
        query = f"{city} {country_term} {_SEARCH_TERMS.get(preference, 'museum')} official"
        body = {"query": query, "engineType": "GlobalBasic",
                "contents": {"mainText": True, "markdownText": False,
                             "summary": False, "rerankScore": True},
                "advancedParams": {"numResults": 12,
                                   "excludeSites": ",".join(_BLOCKED_SOURCE_HOSTS)}}
        try:
            with httpx.Client(timeout=httpx.Timeout(8.0, connect=3.0),
                              transport=self._transport, follow_redirects=False) as client:
                with client.stream("POST", _SEARCH_URL,
                                   headers={"Authorization": f"Bearer {self._api_key}"},
                                   json=body) as response:
                    if response.status_code in (401, 403):
                        raise RecommendationSearchError("authentication")
                    if response.status_code == 429:
                        raise RecommendationSearchError("limited")
                    if response.status_code != 200:
                        raise RecommendationSearchError("upstream")
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > 256_000:
                            raise RecommendationSearchError("invalid")
                    payload = httpx.Response(200, content=bytes(data)).json()
        except httpx.TimeoutException as exc:
            raise RecommendationSearchError("timeout") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise RecommendationSearchError("upstream") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("pageItems"), list):
            raise RecommendationSearchError("invalid")
        context = payload.get("queryContext")
        if isinstance(context, dict) and context.get("engineType") not in (None, "GlobalBasic"):
            raise RecommendationSearchError("invalid")
        stamp = datetime.now(timezone.utc).isoformat()
        records = []
        for item in payload["pageItems"][:12]:
            record = _search_record(item, city, preference, stamp, country)
            if self._diagnostics is not None and isinstance(item, dict):
                self._diagnostics.append({
                    "title": str(item.get("title", ""))[:120],
                    "url": item.get("link") if _public_url(item.get("link")) else None,
                    "snippet": str(item.get("snippet", ""))[:300],
                    "main_text_length": len(item.get("mainText") or ""),
                    "accepted": record is not None,
                })
            if record:
                records.append(record)
        return sorted(records, key=lambda row: _source_rank(row["url"]))


class ReviewedCatalogProvider:
    """Only versioned place facts reach the answer, even when live search runs."""

    def __init__(self, live: CleverSeeRecommendationProvider | None = None):
        self._live = live
        self.live_status = "disabled"

    def search(self, city: str, preference: str | None,
               country: dict) -> list[dict]:
        places = places_for_city(city, country["code"], preference)
        if self._live is not None and places:
            try:
                candidates = self._live.search(city, preference, country)
                names = {name.casefold() for place in places
                         for name in (place["name"], place["display_name_zh"],
                                      *place["aliases"])}
                self.live_status = ("matched" if any(
                    isinstance(item, dict) and
                    isinstance(item.get("place"), str) and
                    item["place"].casefold() in names for item in candidates)
                    else "unverified")
            except RecommendationSearchError as exc:
                self.live_status = exc.kind
            except Exception:
                self.live_status = "upstream"
            logger.info("place_search status=%s", self.live_status)
        return places


def _search_record(item: object, city: str, preference: str | None,
                   stamp: str, country: dict | None = None) -> dict | None:
    country = country or _known_country(city)
    if not _valid_country(country):
        return None
    if not isinstance(item, dict):
        return None
    title, snippet, link = (item.get(name) for name in ("title", "snippet", "link"))
    if not all(isinstance(value, str) for value in (title, snippet, link)):
        return None
    title, snippet = title.strip()[:160], " ".join(snippet.split())[:500]
    main_text = item.get("mainText")
    main_text = (" ".join(main_text.split())[:3000]
                 if isinstance(main_text, str) else "")
    tags = item.get("tags")
    if item.get("correlationTag") == 0:
        return None
    if (isinstance(tags, dict) and (str(tags.get("isUgc", "")).lower() == "true"
                                    or str(tags.get("isListPage", "")).lower() == "true")):
        return None
    if (re.search(r"[<>\[\]{}\x00-\x1f\x7f]", title)
            or _INSTRUCTION_TEXT.search(title) or _INSTRUCTION_TEXT.search(snippet)):
        return None
    city_text = f"{title} {snippet} {main_text}"
    aliases = [alias for alias, name in _CITY_ALIASES.items() if name == city]
    if (not _public_url(link)
            or not (_mentioned(city_text, city)
                    or any(alias in city_text for alias in aliases))):
        return None
    host = urlsplit(link).hostname or ""
    if (_blocked_source(host)
            or urlsplit(link).path.lower().startswith(("/directory/", "/search/"))):
        return None
    if (any(_mentioned(city_text, term) for term in _CONFLICTING_REGIONS.get(city, ()))
            or not _country_evidence(city_text, host, country)):
        return None
    title_parts = re.split(r"\s+[|–—-]\s+|\s*[:：]\s*", title)
    place = next((part.strip() for part in title_parts
                  if part.strip() and not _GENERIC_PLACE.search(part.strip())), "")
    if place.casefold().endswith(f", {city.casefold()}"):
        place = place[:-(len(city) + 2)]
    chinese_alias = next((alias for alias, name in _CITY_ALIASES.items()
                          if name == city), "")
    generic_chinese = bool(chinese_alias and (
        place == f"{chinese_alias}历史博物馆"
        or place in {f"{chinese_alias}{suffix}"
                     for suffix in ("市", "都", "府", "县", "區", "区")}
    ))
    excerpt = snippet
    if not _mentioned(excerpt, place) and main_text:
        match = re.search(re.escape(place), main_text, re.I)
        if match:
            excerpt = main_text[max(0, match.start() - 60):match.end() + 360]
    if (not _mentioned(excerpt, place) and preference == "history"
            and _HISTORY_VENUE.search(place)
            and _mentioned(snippet, city)
            and any(_mentioned(snippet, term) for term in _CATEGORY_TERMS["history"])):
        excerpt = f"{place}. {snippet}"[:500]
    location = re.search(r"\bLocation\s*\|\s*([^|]{3,100})", excerpt, re.I)
    wrong_city_location = bool(location and not (
        _mentioned(location.group(1), city)
        or any(alias in location.group(1) for alias in aliases)))
    if (not 2 <= len(place) <= 100 or not _PLACE.fullmatch(place)
            or place.casefold() in city.casefold() or place in aliases
            or place.casefold() == f"{city.casefold()} city"
            or place.casefold() in {"museum", "gallery", "park", "zoo", "market",
                                    "national", country["name"].casefold()}
            or (" " not in place and place[:1].islower())
            or _GENERIC_PLACE.search(place) or _NON_VENUE.search(place)
            or _STORY_TITLE.search(place) or _DESCRIPTIVE_TITLE.search(place)
            or _AWAY_FROM_CITY.search(excerpt)
            or generic_chinese or wrong_city_location
            or (preference == "history" and not _HISTORY_VENUE.search(place))
            or (preference in _THEME_VENUE
                and not _THEME_VENUE[preference].search(place))
            or not _mentioned(excerpt, place)
            or _INSTRUCTION_TEXT.search(excerpt)):
        return None
    supported = [category for category, terms in _CATEGORY_TERMS.items()
                 if any(_mentioned(f"{title} {excerpt}", term) for term in terms)]
    if not supported:
        return None
    category = preference if preference in supported else supported[0]
    return {"city": city, "place": place, "category": category,
            "title": title, "url": link, "excerpt": excerpt,
            "retrieved_at": stamp}


def _known_country(city: str) -> dict | None:
    return country_for_fixture_city(city)


def _valid_country(country: object) -> bool:
    return (isinstance(country, dict)
            and isinstance(country.get("code"), str)
            and isinstance(country.get("name"), str)
            and country["name"] in {entry["country"] for entry in airport_geography().values()
                                    if entry["country_code"] == country["code"]})


def _country_evidence(text: str, host: str, country: dict) -> bool:
    code = country["code"]
    terms = (country["name"], *_COUNTRY_ALIASES.get(code, ()))
    if any(_mentioned(text, term) for term in terms):
        return True
    suffixes = ("uk",) if code == "GB" else (code.lower(),)
    if code == "US":
        suffixes += ("gov",)
    return any(host.endswith(f".{suffix}") for suffix in suffixes)


def _blocked_source(host: str) -> bool:
    return any(host == name or host.endswith(f".{name}")
               for name in _BLOCKED_SOURCE_HOSTS)


def _source_rank(url: str) -> int:
    host = urlsplit(url).hostname or ""
    if host.endswith((".gov", ".gov.uk")) or any(
            token in host for token in ("museum", "museo", "tourism", "visit")):
        return 0
    if host.endswith(".wikipedia.org"):
        return 1
    return 2


def reachable_cities(db) -> list[str]:
    """Cities with at least one scheduled, offered flight in the demo window."""
    query = (select(airports.c.city).distinct()
             .join(flights, flights.c.arrival_airport == airports.c.code)
             .join(flight_offers, flight_offers.c.flight_id == flights.c.id)
             .where(flights.c.status == "Scheduled"))
    return sorted(db.execute(query).scalars().all(), key=str.casefold)


def _owned_destinations(db, traveler_id: str) -> list[str]:
    now = datetime.now(timezone.utc)
    flight_cities = db.execute(
        select(airports.c.city)
        .join(flights, flights.c.arrival_airport == airports.c.code)
        .join(flight_orders, flight_orders.c.flight_id == flights.c.id)
        .where(flight_orders.c.traveler_id == traveler_id,
               flight_orders.c.status == "confirmed",
               flights.c.arrival_at > now)
        .order_by(flights.c.arrival_at).limit(10)
    ).scalars().all()
    hotel_cities = db.execute(
        select(hotels.c.city)
        .join(hotel_products, hotel_products.c.hotel_id == hotels.c.id)
        .join(hotel_orders, hotel_orders.c.product_id == hotel_products.c.id)
        .where(hotel_orders.c.traveler_id == traveler_id,
               hotel_orders.c.status == "confirmed", hotel_orders.c.check_out > now.date())
        .order_by(hotel_orders.c.check_in).limit(10)
    ).scalars().all()
    return list(dict.fromkeys([*flight_cities, *hotel_cities]))


def _public_url(value: object) -> bool:
    if (not isinstance(value, str) or len(value) > 500
            or any(c.isspace() or c in "[]()<>\\" for c in value)):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    host = parsed.hostname
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        return False
    if host == "localhost" or host.endswith((".local", ".internal")) or "." not in host:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        return bool(re.fullmatch(r"[A-Za-z0-9.-]+", host))


def _clean_evidence(record: object, city: str) -> dict | None:
    if not isinstance(record, dict) or record.get("city") != city:
        return None
    place, title, url = (record.get(key) for key in ("place", "title", "url"))
    excerpt = record.get("excerpt")
    if (not all(isinstance(value, str) for value in (place, title))
            or not 2 <= len(place.strip()) <= 100 or not _PLACE.fullmatch(place.strip())
            or not 2 <= len(title.strip()) <= 160 or "\n" in title or "\r" in title
            or not isinstance(excerpt, str) or not 10 <= len(excerpt) <= 500
            or place.casefold() not in excerpt.casefold() or _UNSAFE_TEXT.search(excerpt)
            or _UNSAFE_TEXT.search(place) or _UNSAFE_TEXT.search(title)
            or not _public_url(url)):
        return None
    category = record.get("category")
    if not isinstance(category, str) or category not in _CATEGORIES:
        return None
    try:
        stamp = datetime.fromisoformat(record.get("retrieved_at"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        return None
    stamp = stamp.astimezone(timezone.utc)
    if stamp > datetime.now(timezone.utc):
        return None
    return {"place": place.strip(), "title": title.strip(), "url": url,
            "city": city, "category": category,
            "retrieved_at": stamp.isoformat()}


def _mentioned(text: str, name: str) -> bool:
    if name.isascii():
        return bool(re.search(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", text, re.I))
    return name in text


def _place_key(name: str) -> str:
    return re.sub(r"^the\s+", "", name.casefold()).strip()


def _url_key(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.hostname}{parsed.path.rstrip('/').casefold()}"


def _has_cjk(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", value))


def _same_place(left: dict, right: dict, city: str) -> bool:
    a, b = _place_key(left["place"]), _place_key(right["place"])
    if a == b or _url_key(left["url"]) == _url_key(right["url"]):
        return True
    short, long = sorted((a, b), key=len)
    if (not _has_cjk(short) and len(short) >= 8 and long.endswith(f" {short}")
            and len(long) - len(short) <= len(city) + 1):
        return True
    return _has_cjk(a) != _has_cjk(b)


def _explicit_city(user_text: str, candidates: list[str]) -> str | None:
    matches = [city for city in candidates if _mentioned(user_text, city)]
    matches.extend(city for alias, city in _CITY_ALIASES.items()
                   if alias in user_text and city in candidates)
    return max(matches, key=len) if matches else None


def read_recommendation(db, traveler_id: str, user_text: str, params: dict,
                        provider: RecommendationProvider | None = None) -> dict:
    """The model can suggest a city, but cannot assert ownership or evidence."""
    excursion_action = bool(re.search(r"景点|游览|门票", user_text)
                            and re.search(r"预订|订|购买|购票|买|预约|改期|取消", user_text))

    def result(answer, references=()):
        if excursion_action:
            answer = "当前不支持游览预订、改期或取消。" + answer
        return {"answer": answer, "references": list(references)}

    cities = reachable_cities(db)
    if not cities:
        return result("当前航班网络没有可核实的可达城市，暂无法推荐地点。")
    as_of = db.execute(select(demo_state.c.as_of).where(
        demo_state.c.id == 1)).scalar_one_or_none() or date.today()
    owned = [city for city in _owned_destinations(db, traveler_id) if city in cities]
    candidate = params.get("city") if isinstance(params.get("city"), str) else None
    outbound = bool(re.search(r"其他国家|别的国家|国外|出国|境外", user_text)
                    or ("中国" in user_text and re.search(r"其他地方|别的地方", user_text)
                        and re.search(r"我.{0,8}在中国", user_text)))
    if outbound:
        source = params.get("source_city")
        source = source if isinstance(source, str) and len(source) <= 80 else None
        if source is None:
            source = _explicit_city(user_text, cities)
        if source:
            source_codes = db.execute(select(airports.c.code).where(
                airports.c.city == source)).scalars().all()
            choices = db.execute(select(airports.c.city).distinct().join(
                flights, flights.c.arrival_airport == airports.c.code).join(
                flight_offers, flight_offers.c.flight_id == flights.c.id).where(
                flights.c.departure_airport.in_(source_codes), flights.c.status == "Scheduled"
            )).scalars().all()
            source_country = country_for_fixture_city(source)
        else:
            choices = cities
            source_country = {"code": "CN"} if "中国" in user_text else None
        if source_country:
            choices = [name for name in choices if
                       (country_for_fixture_city(name) or {}).get("code") != source_country["code"]]
        else:
            choices = []
        preferred = ("Tokyo", "Seoul", "London", "Paris", "Amsterdam", "Singapore",
                     "New York", "Sydney", "Bangkok", "Dubai")
        choices = sorted(set(choices), key=lambda name: (name not in preferred,
                         preferred.index(name) if name in preferred else name))[:5]
        if not choices:
            return result("目前无法核实从你所在城市出发的境外模拟航线；请说明出发城市。")
        basis = f"从{city_zh(source)}出发的直达航线" if source else "当前可达城市"
        return result(f"按本平台的模拟航班网络，{basis}可考虑："
                      f"{'、'.join(city_zh(name) for name in choices)}。"
                      "你偏好历史、自然还是美食？选定城市后，我可以介绍有来源的经典地点；"
                      "具体出发日期的航班仍需单独查询。")
    # A city proposed by the model is only accepted when the traveler typed it.
    explicit = _explicit_city(user_text, cities)
    place_cities = set()
    if explicit is None:
        try:
            place_cities = {place["city"] for place in reviewed_places()
                            if place["city"] in cities and any(
                                len(name) >= 2 and _mentioned(user_text, name)
                                for name in (place["display_name_zh"], place["name"],
                                             *place["aliases"]))}
        except (OSError, ValueError):
            return result("地点目录校验异常，暂无法核实该地点的城市。")
        if len(place_cities) > 1:
            return result("你提到了不同城市的地点，请明确本轮要了解哪座城市。")
    requested_country = next(((name, code) for name, code in _COUNTRY_REQUESTS.items()
                              if name in user_text), None)
    if requested_country and explicit is None:
        name, code = requested_country
        choices = [place for place in cities if
                   (country_for_reachable_city(db, place) or {}).get("code") == code]
        if not choices:
            return result(f"当前演示航班网络没有可核实的{name}可达城市；请换一个目的地。")
        shown = "、".join(city_zh(place) for place in choices[:8])
        suffix = "等" if len(choices) > 8 else ""
        return result(f"当前可推荐的{name}航班可达城市有：{shown}{suffix}。"
                      "你想了解哪座城市？选定后我会给出有来源的经典地点建议。")
    if requested_country and explicit and (
            (country_for_reachable_city(db, explicit) or {}).get("code")
            != requested_country[1]):
        return result("你提到的城市与国家或地区不一致，请明确本轮目的地。")
    resolved = params.get("_resolved_city")
    resolved = resolved if isinstance(resolved, str) and resolved in cities else None
    city = explicit or next(iter(place_cities), None) or resolved
    candidate_mentioned = bool(candidate and _mentioned(user_text, candidate))
    if city is None and candidate_mentioned and candidate.isascii():
        alternatives = "、".join(city_zh(name) for name in (owned[:3] or cities[:3]))
        return result(f"该城市不在当前航班可达城市范围内。可考虑 {alternatives}；请指定其中一座城市后再查询有来源的地点。")
    if city is None and candidate_mentioned:
        return result("暂无法匹配这个城市的航班资料；请补充城市或机场名称。")
    if city is None and candidate:
        return result("无法核对本轮目的地；请明确城市或机场名称。")
    if city is None:
        if len(owned) == 1:
            city = owned[0]
        elif len(owned) > 1:
            return result(f"你的已确认行程涉及 {'、'.join(city_zh(name) for name in owned[:5])}。请指定想了解哪座城市的经典地点。")
        else:
            return result(f"请指定目的地城市；当前可达城市例如 {'、'.join(city_zh(name) for name in cities[:3])}。地点需有来源后才能推荐。")
    mentioned = {name for name in cities if _mentioned(user_text, name)}
    mentioned.update(name for alias, name in _CITY_ALIASES.items()
                     if alias in user_text and name in cities)
    if len(mentioned) > 1:
        return result("你提到了多座城市，请指定本轮要推荐哪一座。")
    country = country_for_reachable_city(db, city)
    if country is None:
        return result(f"{city_zh(city)} 的国家或地区无法从当前航班资料唯一核实，暂不联网推荐地点。")
    preference = next((category for word, category in _PREFERENCES.items()
                       if word in user_text), None)
    source = provider or get_recommendation_provider()
    catalog = isinstance(source, ReviewedCatalogProvider)
    failure = "unconfigured" if isinstance(source, UnavailableRecommendationProvider) else None
    try:
        raw = source.search(city, preference, country)
    except RecommendationSearchError as exc:
        raw, failure = [], exc.kind
    except Exception:
        raw, failure = [], "catalog" if catalog else "upstream"
    if not isinstance(raw, list):
        raw = []
    evidence = []
    if catalog:
        evidence = [{**item, "place": item["name"], "title": item["name"]}
                    for item in raw if isinstance(item, dict)]
    else:
        for item in raw[:10]:
            clean = _clean_evidence(item, city)
            if not clean:
                continue
            if any(_same_place(clean, old, city) for old in evidence):
                continue
            evidence.append(clean)
        if preference:
            evidence.sort(key=lambda item: item["category"] != preference)
    if not evidence:
        if catalog and preference and failure is None:
            return result(f"{city_zh(city)} 暂无近期核查的{_CATEGORIES[preference]}主题地点；请换个偏好或城市。")
        reasons = {
            "unconfigured": "尚未配置联网搜索",
            "authentication": "联网搜索认证或服务开通异常",
            "limited": "联网搜索已限流或达到额度",
            "timeout": "联网搜索超时",
            "upstream": "联网搜索暂时不可用",
            "invalid": "联网搜索结果格式异常",
            "catalog": "地点目录校验异常",
        }
        detail = reasons.get(failure, "当前来源不足")
        answer = (f"{city_zh(city)} 在当前航班网络覆盖内，但{detail}，目前无法核实经典地点的可靠来源，"
                  "暂不编造地点信息。")
        return result(answer)
    prefix = f"根据已核对的来源，{city_zh(city)} 可考虑以下地点（仅供旅行建议）："
    if catalog:
        reviewed_on = max(item["reviewed_on"] for item in evidence)
        prefix = f"根据截至 {reviewed_on} 人工核查的地点资料，{city_zh(city)} 可考虑（仅供旅行建议）："
        if source.live_status not in ("disabled", "matched"):
            prefix = "实时检索未能核实新资料，已回退至人工核查目录。" + prefix
    if getattr(source, "test_only", False):
        prefix = "固定证据测试输出，地点和链接仅用于验收，不能作为实时旅行资料。" + prefix
    if city in owned:
        prefix = f"结合你已确认的 {city_zh(city)} 行程，" + prefix
    if ("附近" in user_text or "周边" in user_text) and resolved:
        prefix = (f"按你指定的 {city_zh(city)}，以下是城市级地点建议；"
                  "当前没有你的位置或景点坐标，不能核实距离。" + prefix)
    if "酒店" in user_text and re.search(r"附近|距离|多远|几公里|公里|步行|车程", user_text):
        prefix += "目前缺少已核实的地点坐标，无法判断这些地点与酒店的距离。"
    if re.search(r"门票|票价|价格|多少钱|费用", user_text):
        prefix += "地点目录不含票价，无法核实当前门票金额；请以场馆官方信息为准。"
    if re.search(r"营业时间|开放时间|开门|关门|几点开|几点关", user_text):
        prefix += "地点目录不含实时开放时间，无法核实当前营业安排；请以场馆官方信息为准。"
    lines = [prefix]
    refs = []
    # One live venue avoids translating the same venue into multiple apparent choices.
    limit = 1 if getattr(source, "live_source", False) else 3
    for index, item in enumerate(evidence[:limit], start=1):
        label = _CATEGORIES[item["category"]]
        reason = f"符合你提出的{label}偏好" if item["category"] == preference else f"可了解当地{label}主题"
        if catalog:
            display = item["display_name_zh"]
            label_name = (display if display == item["place"] else
                          f"{display}（{item['place']}）")
            lines.append(f"{index}. {label_name}：{item['intro']}（{reason}）")
            refs.append({"type": "place_source", "id": item["id"],
                         "city": city, "country_code": country["code"],
                         "place": item["place"], "title": item["title"],
                         "display_name": display,
                         "url": item["url"], "source_class": item["source_class"],
                         "reviewed_on": item["reviewed_on"],
                         "display_reviewed_on": item["display_reviewed_on"]})
            continue
        title = item["title"].replace("[", "\\[").replace("]", "\\]")
        lines.append(f"{index}. {item['place']}：{reason}。来源：[{title}]({item['url']})；检索于 {item['retrieved_at']}。")
        refs.append({"type": "place_source", "city": city, "place": item["place"],
                     "title": item["title"], "url": item["url"],
                     "retrieved_at": item["retrieved_at"]})
    return result("\n".join(lines), refs)
