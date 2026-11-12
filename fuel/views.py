import logging

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.http import urlencode
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView, exception_handler

from fuel.services.planner import PlanError, build_plan
from fuel.services.stations import DatasetChanging, get_index

RETRY_AFTER_SECONDS = "5"


def _error_response(exc: PlanError, map_url=None):
    body = {"error": str(exc)}
    if exc.code:
        body["code"] = exc.code
    if exc.partial:  # e.g. no stations along the route: still return the route and its map
        body.update(exc.partial)
        if map_url:
            body["map_url"] = map_url
    response = Response(body, status=exc.status)
    if exc.status == 503:
        response["Retry-After"] = RETRY_AFTER_SECONDS
    return response


def api_exception_handler(exc, context):
    """DRF errors (validation, throttling) in the same {"error": ...} shape as ours."""
    response = exception_handler(exc, context)
    if response is not None:
        details = response.data
        message = details.get("detail") if isinstance(details, dict) and "detail" in details else "Invalid request."
        response.data = {"error": str(message)}
        if not (isinstance(details, dict) and set(details) == {"detail"}):
            response.data["details"] = details
    return response


class RouteQuerySerializer(serializers.Serializer):
    start = serializers.CharField(max_length=200, help_text='"City, ST", an address, or "lat,lon"')
    finish = serializers.CharField(max_length=200)
    geometry = serializers.BooleanField(required=False, default=True)
    min_stop_savings = serializers.FloatField(
        required=False, allow_null=True, default=None, min_value=0, max_value=1000,
        help_text="Skip stops that save less than this many dollars. Default 5; 0 = strictly cheapest plan.",
    )
    start_fuel_gallons = serializers.FloatField(
        required=False, allow_null=True, default=None, min_value=0,
        help_text="Fuel already in the tank at the start (gallons, not paid for). Default: start empty and pay for all fuel.",
    )


class RoutePlanView(APIView):
    """
    Route between two US locations with cost-effective fuel stops
    (strictly cheapest with min_stop_savings=0).

    GET  /api/route/?start=Chicago, IL&finish=Dallas, TX
    POST /api/route/  {"start": "...", "finish": "..."}
    """

    def get(self, request):
        return self._plan(request, request.query_params)

    def post(self, request):
        return self._plan(request, request.data)

    def _plan(self, request, params):
        q = RouteQuerySerializer(data=params)
        q.is_valid(raise_exception=True)
        start, finish = q.validated_data["start"], q.validated_data["finish"]
        map_url = request.build_absolute_uri(
            reverse("route-map")
            + "?"
            + urlencode(
                {
                    "start": start,
                    "finish": finish,
                    **({"min_stop_savings": q.validated_data["min_stop_savings"]}
                       if q.validated_data["min_stop_savings"] is not None else {}),
                    **({"start_fuel_gallons": q.validated_data["start_fuel_gallons"]}
                       if q.validated_data["start_fuel_gallons"] is not None else {}),
                }
            )
        )
        try:
            data = build_plan(
                start,
                finish,
                include_geometry=q.validated_data["geometry"],
                min_stop_savings=q.validated_data["min_stop_savings"],
                start_fuel_gallons=q.validated_data["start_fuel_gallons"],
            )
        except PlanError as exc:
            return _error_response(exc, map_url)
        data["map_url"] = map_url
        return Response(data)


def healthz(request):
    """Liveness/readiness: the station data is loaded and the in-memory index builds."""
    try:
        index = get_index()
    except DatasetChanging:
        return JsonResponse({"status": "reloading"}, status=503)
    except Exception:  # noqa: BLE001 - any failure here means "not ready"
        logging.getLogger("fuel").exception("Health check failed")
        return JsonResponse({"status": "error"}, status=503)
    if not index.info:
        return JsonResponse({"status": "no station data; run load_stations"}, status=503)
    return JsonResponse({"status": "ok", "stations": len(index.info), "dataset_version": index.version})


def route_map(request):
    """HTML map of the route and fuel stops (Leaflet + OpenStreetMap tiles)."""
    start = request.GET.get("start", "").strip()
    finish = request.GET.get("finish", "").strip()
    savings = request.GET.get("min_stop_savings", "").strip()
    start_fuel = request.GET.get("start_fuel_gallons", "").strip()
    ctx = {"start": start, "finish": finish, "min_stop_savings": savings, "start_fuel_gallons": start_fuel,
           "default_start_fuel": settings.DEFAULT_START_FUEL_GALLONS,
           "plan": None, "route_only": None, "error": None}
    status = 200
    throttle = AnonRateThrottle()  # the page plans routes too, so it shares the API's limit
    if start and finish and not throttle.allow_request(request, None):
        ctx["error"], status = "Too many requests; please wait a moment.", 429
    elif start and finish:
        try:
            ctx["plan"] = build_plan(
                start,
                finish,
                min_stop_savings=float(savings) if savings else None,
                start_fuel_gallons=float(start_fuel) if start_fuel else None,
            )
        except PlanError as exc:
            ctx["error"], status = str(exc), exc.status
            ctx["route_only"] = exc.partial  # draw the route even when there is no fuel plan
        except ValueError:
            ctx["error"], status = "min_stop_savings and start_fuel_gallons must be numbers.", 400
    response = render(request, "fuel/map.html", ctx, status=status)
    if status in (429, 503):
        response["Retry-After"] = str(int(throttle.wait() or 0) or RETRY_AFTER_SECONDS) if status == 429 else RETRY_AFTER_SECONDS
    return response
