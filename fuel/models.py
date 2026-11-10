from django.db import models


class Place(models.Model):
    """A US city/town with its centroid (from the US Census Gazetteer files).

    Used to geocode fuel stations (the price file has no coordinates) and to
    resolve "City, ST" inputs without calling an external geocoding API.
    """

    key = models.CharField(max_length=128, db_index=True)  # normalized "name|ST"
    name = models.CharField(max_length=128)
    state = models.CharField(max_length=2)
    lat = models.FloatField()
    lon = models.FloatField()
    # Lower is better when several places share a normalized name
    # (e.g. an incorporated city beats a CDP of the same name).
    rank = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["key", "rank"]

    def __str__(self):
        return f"{self.name}, {self.state}"


class FuelStation(models.Model):
    opis_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=128)
    state = models.CharField(max_length=2)
    price = models.DecimalField(max_digits=7, decimal_places=4)
    lat = models.FloatField()
    lon = models.FloatField()

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state}) ${self.price}"
