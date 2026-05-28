import math


def erlang_c(agents, traffic_intensity):
    """Calculate Erlang C probability (probability a call waits)."""
    if agents <= traffic_intensity:
        return 1.0
    erlang_b_inv = 1.0
    for i in range(1, agents + 1):
        erlang_b_inv = 1.0 + erlang_b_inv * i / traffic_intensity
    erlang_b = 1.0 / erlang_b_inv
    return (agents * erlang_b) / (agents - traffic_intensity * (1 - erlang_b))


def service_level(agents, calls_per_hour, avg_handle_time, target_answer_time):
    """Calculate service level % given number of agents."""
    traffic_intensity = (calls_per_hour / 3600) * avg_handle_time
    if agents <= traffic_intensity:
        return 0.0
    ec = erlang_c(agents, traffic_intensity)
    mu = 1 / avg_handle_time
    sl = 1 - ec * math.exp(-(agents - traffic_intensity) * mu * target_answer_time)
    return round(max(0.0, min(1.0, sl)) * 100, 2)


def occupancy(agents, calls_per_hour, avg_handle_time):
    """Calculate agent occupancy percentage."""
    traffic_intensity = (calls_per_hour / 3600) * avg_handle_time
    return round((traffic_intensity / agents) * 100, 2) if agents > 0 else 0


def agents_required(calls_per_hour, avg_handle_time, target_service_level, target_answer_time):
    """Find minimum agents needed to meet target service level."""
    if calls_per_hour <= 0 or avg_handle_time <= 0:
        return 1
    traffic_intensity = (calls_per_hour / 3600) * avg_handle_time
    agents = max(1, math.ceil(traffic_intensity) + 1)
    while agents <= 1000:
        sl = service_level(agents, calls_per_hour, avg_handle_time, target_answer_time)
        if sl >= target_service_level:
            return agents
        agents += 1
    return agents


def parse_aht(aht_str):
    """Parse HH:MM:SS or HH:MM:SS.mmm string to integer seconds."""
    if not aht_str:
        return 0
    aht_str = str(aht_str).split('.')[0].strip()
    parts = aht_str.split(':')
    try:
        h = int(parts[0]) if len(parts) > 0 and parts[0] else 0
        m = int(parts[1]) if len(parts) > 1 and parts[1] else 0
        s = int(parts[2]) if len(parts) > 2 and parts[2] else 0
        return h * 3600 + m * 60 + s
    except (ValueError, IndexError):
        return 0


def hour_label(hour):
    """Convert 0-23 integer to '8:00 AM – 9:00 AM' style label."""
    def fmt(h):
        suffix = 'AM' if h < 12 else 'PM'
        h12 = h % 12 or 12
        return f"{h12}:00 {suffix}"
    return f"{fmt(hour)} – {fmt((hour + 1) % 24)}"


def format_aht(seconds):
    """Convert seconds to '7m 00s' display string."""
    m = seconds // 60
    s = seconds % 60
    return f"{m}m {s:02d}s"


def calculate_staffing(rows, target_sl_pct, target_seconds, shrinkage_pct, aht_seconds):
    """
    Run Erlang C on each row and return enriched results.

    rows: list of {day, hour, avg_calls}
    aht_seconds: global average handle time in seconds, applied to all rows
    Returns same list with calculated fields added.
    """
    shrinkage = shrinkage_pct / 100.0

    result = []
    for row in rows:
        calls = row['avg_calls']

        if calls <= 0 or aht_seconds <= 0:
            n_req = 1
            n_shrink = 1
            sl_achieved = 100.0
        else:
            n_req = agents_required(calls, aht_seconds, target_sl_pct, target_seconds)
            sl_achieved = service_level(n_req, calls, aht_seconds, target_seconds)
            if 0 < shrinkage < 1:
                n_shrink = math.ceil(n_req / (1 - shrinkage))
            else:
                n_shrink = n_req

        result.append({
            **row,
            'hour_label': hour_label(row['hour']),
            'agents_required': n_req,
            'agents_shrinkage': n_shrink,
            'service_level_achieved': round(sl_achieved, 1),
        })

    return result
