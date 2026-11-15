"""Tech news (HN, RSS, DDGS) and weather (Open-Meteo with disambiguation)."""

from __future__ import annotations

import asyncio
import re

import httpx

import config
from tools.base import ToolContext, tool
from tools.web import HEADERS

FEEDS = {
    "The Verge": "https://www.theverge.com/rss/index.xml",
    "Ars Technica": "https://feeds.arstechnica.com/arstechnica/index",
    "TechCrunch": "https://techcrunch.com/feed/",
}


async def _hn(n: int = 10) -> list[str]:
    base = "https://hacker-news.firebaseio.com/v0"
    async with httpx.AsyncClient(timeout=15) as c:
        ids = (await c.get(f"{base}/topstories.json")).json()[:n]
        resps = await asyncio.gather(*[c.get(f"{base}/item/{i}.json") for i in ids])
    lines = []
    for r in resps:
        it = r.json() or {}
        link = it.get("url") or f"https://news.ycombinator.com/item?id={it.get('id')}"
        lines.append(f"[HN {it.get('score', 0)}pts] {it.get('title')} - {link}")
    return lines


async def _rss(name: str, url: str, n: int = 5) -> list[str]:
    import feedparser

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=15) as c:
        r = await c.get(url)
    feed = feedparser.parse(r.text)
    return [f"[{name}] {e.title} - {e.link}" for e in feed.entries[:n]]


@tool(
    "tech_news",
    "Get the latest tech news (Hacker News + The Verge, Ars Technica, TechCrunch). "
    "Optionally pass a topic (e.g. 'AI', 'Apple', 'cybersecurity') for focused news.",
    {"topic": {"type": "string", "description": "Optional topic filter."}},
)
async def tech_news(ctx: ToolContext, topic: str | None = None) -> str:
    if topic:

        def _news():
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS
            return list(DDGS(timeout=7).news(topic, max_results=8))

        items = await asyncio.to_thread(_news)
        return (
            "\n".join(
                f"[{i.get('source')}] {i.get('title')} - {i.get('url')}" for i in items
            )
            or "No news found."
        )

    jobs = [_hn(10)] + [_rss(n, u) for n, u in FEEDS.items()]
    results = await asyncio.gather(*jobs, return_exceptions=True)
    lines = []
    for r in results:
        if not isinstance(r, Exception):
            lines.extend(r)
    return "\n".join(lines) or "Couldn't reach any news source."


WMO = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    56: "light freezing drizzle",
    57: "dense freezing drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "heavy freezing rain",
    71: "slight snow fall",
    73: "moderate snow fall",
    75: "heavy snow fall",
    77: "snow grains",
    80: "slight rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    85: "slight snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}

_COUNTRY_HINTS = {
    "canada": ("CA", ("canada", "canadian")),
    "united states": ("US", ("united states", "usa", "us", "american")),
    "united kingdom": ("GB", ("united kingdom", "uk", "britain", "british")),
    "australia": ("AU", ("australia", "australian")),
}


def _weather_location_query(location: str) -> tuple[str, str | None]:
    cleaned = re.sub(r"\s+", " ", location).strip(" ,")
    cleaned = re.sub(r"^the\s+", "", cleaned, flags=re.IGNORECASE)
    folded = cleaned.casefold()
    for country, (code, aliases) in _COUNTRY_HINTS.items():
        for alias in sorted(aliases, key=len, reverse=True):
            if folded == alias:
                return country, code
            if folded.startswith(alias + " "):
                return cleaned[len(alias) :].strip(" ,"), code
            if folded.endswith(" " + alias):
                return cleaned[: -len(alias)].strip(" ,"), code
            if folded.endswith(", " + alias):
                return cleaned[: -(len(alias) + 2)].strip(" ,"), code
    return cleaned, None


def _weather_description(code) -> str:
    try:
        numeric_code = int(code)
    except (TypeError, ValueError):
        return f"Unmapped weather code ({code})"
    return WMO.get(numeric_code, f"Unmapped weather code ({numeric_code})")


@tool(
    "get_weather",
    "Get current weather and today's forecast for a place. If the user names a country or demonym, "
    "use it to disambiguate the place. If a place name matches multiple countries and no country is specified, ask the user to clarify.",
    {
        "location": {
            "type": "string",
            "description": "City or place name, optionally with country (e.g. 'Ontario, Canada'). Defaults to user's home.",
        }
    },
)
async def get_weather(ctx: ToolContext, location: str | None = None) -> str:
    original_location = (location or config.LOCATION).strip()
    search_location, country_code = _weather_location_query(original_location)
    if not search_location:
        return "Please provide a city or place name."

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            geo_params = {"name": search_location, "count": 10, "language": "en", "format": "json"}
            if country_code:
                geo_params["countryCode"] = country_code
            geo_response = await c.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params=geo_params,
            )
            geo_response.raise_for_status()
            geo = geo_response.json()

        results = geo.get("results") or []
        if not results:
            qualifier = f" in {country_code}" if country_code else ""
            return f"I couldn't find '{search_location}'{qualifier}. Please check the place name."

        if not country_code:
            exact_matches = [
                item
                for item in results
                if str(item.get("name", "")).casefold() == search_location.casefold()
            ]
            candidates = exact_matches or results[:5]
            countries = {item.get("country_code") or item.get("country") for item in candidates}
            countries.discard(None)
            by_pop = sorted(candidates, key=lambda x: x.get("population") or 0, reverse=True)
            top_pop = by_pop[0].get("population") or 0
            second_pop = (by_pop[1].get("population") or 0) if len(by_pop) > 1 else 0
            dominant = top_pop >= 500_000 and (second_pop == 0 or top_pop >= second_pop * 10)
            if len(countries) > 1 and not dominant:
                choices = []
                seen = set()
                for item in candidates:
                    label_parts = [item.get("name"), item.get("admin1"), item.get("country")]
                    label = ", ".join(part for part in label_parts if part)
                    if label and label not in seen:
                        choices.append(label)
                        seen.add(label)
                return (
                    f"I found more than one place called '{search_location}':\n"
                    + "\n".join(f"- {choice}" for choice in choices[:5])
                    + "\nWhich one do you mean?"
                )

        if country_code:
            matching = [item for item in results if item.get("country_code") == country_code]
            if matching:
                results = matching
        g = results[0]

        async with httpx.AsyncClient(timeout=15) as c:
            weather_response = await c.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": g["latitude"],
                    "longitude": g["longitude"],
                    "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                    "timezone": "auto",
                    "forecast_days": 1,
                },
            )
            weather_response.raise_for_status()
            w = weather_response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return f"Weather unavailable right now ({type(exc).__name__})."

    cur, day = w.get("current"), w.get("daily")
    required_current = ("temperature_2m", "apparent_temperature", "weather_code", "wind_speed_10m")
    required_daily = ("temperature_2m_max", "temperature_2m_min", "precipitation_probability_max", "weather_code")
    if not isinstance(cur, dict) or not isinstance(day, dict):
        return "Weather unavailable: the forecast provider returned incomplete data."
    if any(key not in cur for key in required_current) or any(not day.get(key) for key in required_daily):
        return "Weather unavailable: the forecast provider returned incomplete data."

    city = g["name"]
    country = g.get("country", "")
    location_label = f"{city}, {country}" if country else city
    current_condition = _weather_description(cur["weather_code"]).capitalize()
    forecast_condition = _weather_description(day["weather_code"][0]).capitalize()

    return (
        f"{location_label}:\n\n"
        f"Now: {cur['temperature_2m']}°C\n"
        f"Feels like: {cur['apparent_temperature']}°C\n"
        f"Conditions: {current_condition}\n"
        f"Wind: {cur['wind_speed_10m']} km/h\n\n"
        f"Today's forecast:\n"
        f"High: {day['temperature_2m_max'][0]}°C\n"
        f"Low: {day['temperature_2m_min'][0]}°C\n"
        f"Rain probability: {day['precipitation_probability_max'][0]}%\n"
        f"Forecast conditions: {forecast_condition}"
    )
