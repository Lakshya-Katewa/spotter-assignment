"""
WSGI config for config project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.1/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

application = get_wsgi_application()

# Open the TLS connection to the routing server now, in the background, so the first
# route request doesn't pay the ~0.35 s handshake. Only here (the server entry point,
# also used by runserver), never in tests or management commands.
from fuel.services.routing import warm_up_connection  # noqa: E402

warm_up_connection()
