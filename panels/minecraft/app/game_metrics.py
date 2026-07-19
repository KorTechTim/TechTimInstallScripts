from datetime import datetime, timezone
import re
from typing import Any


NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
SECTION_CODE_RE = re.compile(r"§.")


def _clean(value: str) -> str:
    return SECTION_CODE_RE.sub("", str(value or ""))


def _values_after_heading(output: str, heading: str, minimum: int) -> list[float]:
    lines = _clean(output).splitlines()
    heading_lower = heading.lower()
    for index, line in enumerate(lines):
        if heading_lower not in line.lower():
            continue
        chunks = [line.split(":", 1)[1] if ":" in line else ""]
        for following in lines[index + 1:index + 4]:
            chunks.append(following)
            values = [float(value) for value in NUMBER_RE.findall(" ".join(chunks))]
            if len(values) >= minimum:
                return values
        values = [float(value) for value in NUMBER_RE.findall(" ".join(chunks))]
        if len(values) >= minimum:
            return values
    return []


def parse_player_counts(output: str, fallback_max: int = 0) -> tuple[int, int]:
    cleaned = _clean(output)
    match = re.search(
        r"There are\s+(\d+)\s+of a max of\s+(\d+)\s+players online",
        cleaned,
        re.IGNORECASE,
    )
    if match:
        return int(match.group(1)), int(match.group(2))
    _, separator, names = cleaned.partition(":")
    online = len([name for name in names.split(",") if name.strip()]) if separator else 0
    return online, max(0, int(fallback_max or 0))


def parse_paper_tps(output: str) -> dict[str, float]:
    values = _values_after_heading(output, "TPS from last 1m, 5m, 15m", 3)
    if len(values) < 3:
        return {}
    return {
        "tps": round(values[0], 2),
        "tps_1m": round(values[0], 2),
        "tps_5m": round(values[1], 2),
        "tps_15m": round(values[2], 2),
    }


def parse_paper_mspt(output: str) -> dict[str, float]:
    values = _values_after_heading(output, "Server tick times", 9)
    if len(values) < 9:
        return {}
    return {
        "mspt": round(values[0], 2),
        "mspt_min": round(values[1], 2),
        "mspt_max": round(values[2], 2),
    }


def parse_spark_tps(output: str) -> dict[str, float]:
    tps_values = _values_after_heading(output, "TPS from last 5s, 10s, 1m, 5m, 15m", 5)
    if len(tps_values) < 5:
        return {}
    metrics = {
        "tps": round(tps_values[0], 2),
        "tps_1m": round(tps_values[2], 2),
        "tps_5m": round(tps_values[3], 2),
        "tps_15m": round(tps_values[4], 2),
    }
    duration_values = _values_after_heading(output, "Tick durations", 8)
    if len(duration_values) >= 8:
        metrics.update({
            "mspt": round(duration_values[1], 2),
            "mspt_min": round(duration_values[0], 2),
            "mspt_p95": round(duration_values[2], 2),
            "mspt_max": round(duration_values[3], 2),
        })
    return metrics


def parse_forge_tps(output: str) -> dict[str, float]:
    matches = re.findall(
        r"Mean tick time:\s*([0-9.]+)\s*ms.*?Mean TPS:\s*([0-9.]+)",
        _clean(output),
        re.IGNORECASE,
    )
    if not matches:
        return {}
    mspt, tps = matches[-1]
    return {"tps": round(float(tps), 2), "mspt": round(float(mspt), 2)}


def parse_tick_query(output: str) -> dict[str, float]:
    cleaned = " ".join(_clean(output).split())
    target_match = re.search(
        r"([0-9]+(?:\.[0-9]+)?)\s*(?:TPS|ticks? per second)",
        cleaned,
        re.IGNORECASE,
    )
    duration_patterns = (
        r"average\s+tick(?:ing)?\s*(?:time|duration)?[^0-9]{0,40}([0-9]+(?:\.[0-9]+)?)\s*(?:ms|milliseconds)",
        r"average\s+(?:time per tick|tick time|tick duration)[^0-9]{0,40}([0-9]+(?:\.[0-9]+)?)\s*(?:ms|milliseconds)",
    )
    duration_match = next(
        (match for pattern in duration_patterns if (match := re.search(pattern, cleaned, re.IGNORECASE))),
        None,
    )
    if not target_match and not duration_match:
        return {}
    target_tps = float(target_match.group(1)) if target_match else 20.0
    mspt = float(duration_match.group(1)) if duration_match else None
    actual_tps = min(target_tps, 1000.0 / mspt) if mspt and mspt > 0 else target_tps
    metrics = {"tps": round(actual_tps, 2)}
    if mspt is not None:
        metrics["mspt"] = round(mspt, 2)
    return metrics


def version_at_least(value: str, minimum: tuple[int, ...]) -> bool:
    text = str(value or "").strip().upper()
    if text == "LATEST":
        return True
    match = re.match(r"(\d+(?:\.\d+)*)", text)
    if not match:
        return False
    parts = tuple(int(part) for part in match.group(1).split("."))
    width = max(len(parts), len(minimum))
    return parts + (0,) * (width - len(parts)) >= minimum + (0,) * (width - len(minimum))


def container_uptime_seconds(started_at: str, now: datetime | None = None) -> int:
    value = str(started_at or "").strip()
    if not value or value.startswith("0001-"):
        return 0
    try:
        started = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0, int((current.astimezone(timezone.utc) - started.astimezone(timezone.utc)).total_seconds()))


def health_state(metrics: dict[str, Any]) -> str:
    tps = metrics.get("tps")
    mspt = metrics.get("mspt")
    if tps is None and mspt is None:
        return "unknown"
    if (tps is not None and float(tps) < 18.0) or (mspt is not None and float(mspt) > 50.0):
        return "critical"
    if (tps is not None and float(tps) < 19.5) or (mspt is not None and float(mspt) > 40.0):
        return "warning"
    return "healthy"
