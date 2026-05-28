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
    """Calculate service level given number of agents."""
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
    traffic_intensity = (calls_per_hour / 3600) * avg_handle_time
    agents = math.ceil(traffic_intensity) + 1
    while agents <= 1000:
        sl = service_level(agents, calls_per_hour, avg_handle_time, target_answer_time)
        if sl >= target_service_level:
            return agents
        agents += 1
    return agents
