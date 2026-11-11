"""Production settings guard (evaluated at import, so checked in a subprocess)."""

import os
import subprocess
import sys
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]


def run_check(**env):
    return subprocess.run(
        [sys.executable, "manage.py", "check"], cwd=ROOT, capture_output=True, text=True,
        env={**os.environ, "DJANGO_SECRET_KEY": "", **env}, timeout=60,
    )


class ProductionSettingsTests(SimpleTestCase):
    def test_debug_off_requires_a_real_secret_key(self):
        env = {k: v for k, v in os.environ.items() if k != "DJANGO_SECRET_KEY"}
        result = subprocess.run([sys.executable, "manage.py", "check"], cwd=ROOT, capture_output=True, text=True,
                                env={**env, "DJANGO_DEBUG": "0"}, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Set DJANGO_SECRET_KEY", result.stderr)

    def test_debug_off_with_secret_key_starts(self):
        result = run_check(DJANGO_DEBUG="0", DJANGO_SECRET_KEY="k8#Qz!w2Lr$9vTn@4pXc&7mYh*3dFg^6sBj%1aRe-Tq5Wz0Uo+Nv7Ki2")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_debug_off_rejects_placeholder_keys(self):
        for key in ("change-me", "x" * 60, "django-insecure-" + "abcdefghijklmnopqrstuvwxyz0123456789"):
            result = run_check(DJANGO_DEBUG="0", DJANGO_SECRET_KEY=key)
            self.assertNotEqual(result.returncode, 0, key)
            self.assertIn("Set DJANGO_SECRET_KEY", result.stderr)


class DeployCheckTests(SimpleTestCase):
    def test_warns_without_shared_cache(self):
        from fuel.checks import shared_cache_for_production

        self.assertEqual([w.id for w in shared_cache_for_production(None)], ["fuel.W001"])
        redis = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": "redis://x"}}
        with self.settings(CACHES=redis):
            self.assertEqual(shared_cache_for_production(None), [])
