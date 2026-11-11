from django.apps import AppConfig


class FuelConfig(AppConfig):
    name = 'fuel'

    def ready(self):
        from fuel import checks  # noqa: F401  (registers the deployment checks)
