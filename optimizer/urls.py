from __future__ import annotations

from django.urls import path

from optimizer import views_api


urlpatterns = [
    # Required API
    path("repos", views_api.repos, name="repos"),
    path("files/<int:repo_id>", views_api.files_for_repo, name="files_for_repo"),
    path("functions/<int:file_id>", views_api.functions_for_file, name="functions_for_file"),
    path("function/<int:fn_id>", views_api.function_detail, name="function_detail"),
    path("function/<int:fn_id>/memory-chart.png", views_api.function_memory_chart_png, name="function_memory_chart_png"),
    path("optimize/<int:function_id>", views_api.optimize_function, name="optimize_function"),
    path("function/<int:fn_id>/decision", views_api.function_decision, name="function_decision"),
    path("file/<int:file_id>/optimize", views_api.optimize_file, name="optimize_file"),
    path("file/<int:file_id>/merged", views_api.file_merged_code, name="file_merged_code"),
    path("file/<int:file_id>/download", views_api.file_download, name="file_download"),
    # Ingestion helpers (upload / GitHub)
    path("repos/ingest/github", views_api.ingest_github, name="ingest_github_v2"),
    path("repos/ingest/file", views_api.ingest_single_file, name="ingest_single_file_v2"),
    # Debug
    path("_debug/module", views_api._debug_module, name="debug_module"),
]

