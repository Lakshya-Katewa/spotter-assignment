from django.contrib import admin

from fuel.models import DatasetVersion, FuelStation, Place


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    list_display = ("opis_id", "name", "city", "state", "price", "location_precision", "exit_label")
    list_filter = ("location_precision", "state")
    search_fields = ("name", "city", "address", "opis_id")
    ordering = ("state", "city")


@admin.register(Place)
class PlaceAdmin(admin.ModelAdmin):
    list_display = ("name", "state", "lat", "lon", "rank")
    search_fields = ("name", "key")
    list_filter = ("state",)


@admin.register(DatasetVersion)
class DatasetVersionAdmin(admin.ModelAdmin):
    list_display = ("version", "station_count", "loaded_at")
