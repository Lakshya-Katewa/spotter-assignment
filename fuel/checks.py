"""Deployment checks (run by `manage.py check --deploy` and at server start)."""

from django.conf import settings
from django.core.checks import Tags, Warning, register


@register(Tags.caches, deploy=True)
def shared_cache_for_production(app_configs, **kwargs):
    """Without Redis each worker process rate-limits and coalesces on its own.

    Under e.g. `gunicorn -w 4` that's up to 4 calls/second to the free OSRM and
    Nominatim servers (their policy is 1/second in total), and "one routing call per
    route" holds only per worker.
    """
    backend = settings.CACHES["default"]["BACKEND"]
    if backend.endswith("LocMemCache"):
        return [Warning(
            "The cache is in-process (LocMemCache): upstream rate limits and request coalescing "
            "are per worker process.",
            hint="Set REDIS_URL (and pip install redis) when running more than one worker process.",
            id="fuel.W001",
        )]
    return []
