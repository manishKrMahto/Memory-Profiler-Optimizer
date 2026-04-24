from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render


def app(request: HttpRequest) -> HttpResponse:
    return render(request, "optimizer/app.html")

