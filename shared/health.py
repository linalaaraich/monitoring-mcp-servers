import time

_start_time = time.monotonic()


def health_response(upstream_name: str, upstream_reachable: bool) -> dict:
    return {
        "status": "healthy" if upstream_reachable else "degraded",
        "uptime_seconds": round(time.monotonic() - _start_time, 1),
        f"{upstream_name}_reachable": upstream_reachable,
    }
