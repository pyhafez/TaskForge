import logging
from http import HTTPStatus

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.http import JsonResponse
from rest_framework import exceptions
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_handler

from taskforge.domain import InvalidTransition


class Conflict(exceptions.APIException):
    status_code = 409
    default_detail = "A resource with these attributes already exists."
    default_code = "conflict"


def problem(status, detail, request=None, errors=None):
    data = {
        "type": f"urn:taskforge:problem:{status}",
        "title": HTTPStatus(status).phrase,
        "status": status,
        "detail": detail,
    }
    if request:
        data.update(instance=request.path, trace_id=getattr(request, "trace_id", ""))
    if errors is not None:
        data["errors"] = errors
    return data


def exception_handler(exc, context):
    request = context.get("request")
    if isinstance(exc, (IntegrityError, InvalidTransition)):
        exc = Conflict(str(exc) if isinstance(exc, InvalidTransition) else Conflict.default_detail)
    if isinstance(exc, DjangoValidationError):
        exc = exceptions.ValidationError(exc.messages)
    response = drf_handler(exc, context)
    if response is None:
        logging.getLogger(__name__).exception("api.unhandled_error")
        response = Response(status=500)
        detail, errors = "An unexpected error occurred.", None
    elif isinstance(exc, exceptions.ValidationError):
        response.status_code = 422
        detail, errors = "Input validation failed.", response.data
    else:
        detail = str(getattr(exc, "detail", HTTPStatus(response.status_code).phrase))
        errors = None
    response.data = problem(response.status_code, detail, request, errors)
    response["Content-Type"] = "application/problem+json"
    # A dedicated renderer preserves the media type through DRF response finalization.
    response.content_type = "application/problem+json"
    return response


def not_found(request, exception=None):
    return JsonResponse(
        problem(404, "Resource not found.", request),
        status=404,
        content_type="application/problem+json",
    )


def server_error(request):
    return JsonResponse(
        problem(500, "An unexpected error occurred.", request),
        status=500,
        content_type="application/problem+json",
    )
