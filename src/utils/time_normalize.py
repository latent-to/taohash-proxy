from datetime import datetime, timezone
from typing import Any, Optional


def ensure_utc(dt: datetime) -> datetime:
    """Return a timezone-aware UTC datetime (assume naive is UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso_z(dt: datetime) -> str:
    """Format a datetime as ISO-8601 with trailing Z and no microseconds."""
    dt_utc = ensure_utc(dt).replace(microsecond=0)
    return dt_utc.isoformat().replace("+00:00", "Z")


def parse_any_ts(value: Any) -> Optional[datetime]:
    """Parse int/float epoch seconds (or ms), ISO string, or datetime → aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1_000_000_000_000:
            ts = ts / 1000.0
        return ensure_utc(datetime.fromtimestamp(ts, tz=timezone.utc))
    if isinstance(value, str):
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            return ensure_utc(datetime.fromisoformat(s))
        except Exception:
            return None
    return None


def normalize_tides_window_snapshot(
    data: dict[str, Any] | None,
    *,
    default_updated_at: Optional[datetime] = None,
) -> dict[str, Any]:
    """Normalize only timestamp fields in a TIDES window snapshot.

    - window_start, window_end → ISO-8601 UTC (Z)
    - updated_at and calculated_at: set to the same ISO-8601 UTC (Z) value. If none present, leave as-is.
    """
    norm = dict(data) if data else {}

    for key in ("window_start", "window_end"):
        if key in norm:
            dt = parse_any_ts(norm.get(key))
            norm[key] = to_iso_z(dt) if dt else None

    ts_source = (
        norm.get("updated_at")
        if norm.get("updated_at") is not None
        else (
            norm.get("calculated_at")
            if norm.get("calculated_at") is not None
            else default_updated_at
        )
    )

    if ts_source is not None:
        dt = parse_any_ts(ts_source)
        iso = to_iso_z(dt) if dt else None
        norm["updated_at"] = iso
        norm["calculated_at"] = iso

    return norm
