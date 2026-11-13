"""`runserver` that sets up the database on first run, so a reviewer only needs
`pip install -r requirements.txt` and `python manage.py runserver`.

On start it applies any unapplied migrations and, if no station data is loaded yet,
runs `load_stations` (~4 s, fully offline: all data ships in data/). Once set up,
this is one quick check per start. Only the development server does this: tests,
other commands and production servers (gunicorn) never change the database on
their own; run `migrate` and `load_stations` there explicitly.
"""

from django.contrib.staticfiles.management.commands.runserver import Command as StaticFilesRunserver
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from fuel.models import FuelStation


def prepare_database(stdout):
    """Apply pending migrations and load the station data if it is missing."""
    executor = MigrationExecutor(connection)
    if executor.migration_plan(executor.loader.graph.leaf_nodes()):
        stdout.write("First run: applying database migrations...")
        call_command("migrate", verbosity=0)
    if not FuelStation.objects.exists():
        stdout.write("First run: loading fuel station data (about 5 seconds, offline)...")
        call_command("load_stations")


class Command(StaticFilesRunserver):
    help = StaticFilesRunserver.help + " Sets up the database on first run (migrate + load_stations)."

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument("--skip-setup", action="store_true",
                            help="Don't migrate or load station data automatically.")

    def handle(self, *args, **options):
        self.skip_setup = options["skip_setup"]
        super().handle(*args, **options)

    def check_migrations(self):
        # Runs in the serving process before it accepts requests (also after autoreload).
        if not getattr(self, "skip_setup", False):
            prepare_database(self.stdout)
        super().check_migrations()
