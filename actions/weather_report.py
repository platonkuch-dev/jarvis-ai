import requests

# WMO weather interpretation codes (the code set Open-Meteo returns) -> a
# short human description. https://open-meteo.com/en/docs -- "WMO Weather
# interpretation codes".
_WMO_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
    56: "light freezing drizzle", 57: "dense freezing drizzle",
    61: "light rain", 63: "moderate rain", 65: "heavy rain",
    66: "light freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "moderate snow", 75: "heavy snow", 77: "snow grains",
    80: "light rain showers", 81: "moderate rain showers", 82: "violent rain showers",
    85: "light snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with light hail", 99: "thunderstorm with heavy hail",
}


def _geocode(city: str) -> tuple[float, float, str] | None:
    """City name -> (latitude, longitude, display label), via Open-Meteo's
    free geocoding API (no key required). None if no match."""
    resp = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": city, "count": 1, "language": "en", "format": "json"},
        timeout=8,
    )
    resp.raise_for_status()
    results = resp.json().get("results") or []
    if not results:
        return None
    r = results[0]
    label_parts = [r["name"]]
    if r.get("admin1"):
        label_parts.append(r["admin1"])
    if r.get("country"):
        label_parts.append(r["country"])
    return r["latitude"], r["longitude"], ", ".join(label_parts)


def weather_action(
    parameters: dict,
    player=None,
    session_memory=None,
) -> str:
    city = parameters.get("city")
    when = (parameters.get("time") or "today").strip().lower()

    if not city or not isinstance(city, str) or not city.strip():
        msg = "Sir, the city is missing for the weather report."
        _log(msg, player)
        return msg
    city = city.strip()

    try:
        geo = _geocode(city)
        if geo is None:
            msg = f"Sir, I couldn't find a location matching '{city}'."
            _log(msg, player)
            return msg
        lat, lon, label = geo

        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min,weather_code",
                "timezone": "auto",
                "forecast_days": 3,
            },
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        msg = f"Sir, I couldn't reach the weather service: {e}"
        _log(msg, player)
        return msg
    except Exception as e:
        msg = f"Sir, something went wrong getting the weather: {e}"
        _log(msg, player)
        return msg

    day_index = 1 if any(w in when for w in ("tomorrow", "yarın", "завтра")) else 0

    if day_index == 0 and "current" in data:
        cur       = data["current"]
        code      = int(cur.get("weather_code", -1))
        condition = _WMO_CODES.get(code, "conditions I don't have a description for")
        temp      = cur.get("temperature_2m")
        feels     = cur.get("apparent_temperature")
        humidity  = cur.get("relative_humidity_2m")
        wind      = cur.get("wind_speed_10m")
        msg = (
            f"Sir, right now in {label} it's {temp:.0f}°C, {condition}, "
            f"feels like {feels:.0f}°C, humidity {humidity:.0f}%, wind {wind:.0f} km/h."
        )
    else:
        daily = data.get("daily", {})
        try:
            code = int(daily["weather_code"][day_index])
            tmax = daily["temperature_2m_max"][day_index]
            tmin = daily["temperature_2m_min"][day_index]
        except (KeyError, IndexError):
            msg = f"Sir, I don't have a forecast for '{when}' in {label}."
            _log(msg, player)
            return msg
        condition = _WMO_CODES.get(code, "conditions I don't have a description for")
        day_label = "tomorrow" if day_index == 1 else when
        msg = f"Sir, {day_label} in {label}: {condition}, high {tmax:.0f}°C, low {tmin:.0f}°C."

    _log(msg, player)

    if session_memory:
        try:
            session_memory.set_last_search(query=f"weather in {city} {when}", response=msg)
        except Exception:
            pass

    return msg


def _log(message: str, player=None) -> None:
    print(f"[Weather] {message}")
    if player:
        try:
            player.write_log(f"JARVIS: {message}")
        except Exception:
            pass
