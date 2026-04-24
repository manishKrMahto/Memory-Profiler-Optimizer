"""
URL configuration for core project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path

from core import views
from optimizer import views_ui

urlpatterns = [
    path('admin/', admin.site.urls),
    # New UI
    path('', views_ui.app, name='app'),
    # Old Phase 1 UI (kept)
    path('phase1/', views.index, name='index_phase1'),
    path('api/repos/ingest', views.ingest_repo, name='ingest_repo'),
    path('api/repos/ingest/github', views.ingest_github, name='ingest_github'),
    path('api/repos/ingest/file', views.ingest_single_file, name='ingest_single_file'),
    path('api/repos/<str:repo_id>/tree', views.repo_tree, name='repo_tree'),
    path('api/repos/<str:repo_id>/file', views.repo_file, name='repo_file'),
    path('optimize-repo/', views.optimize_repo, name='optimize_repo'),
    path('results/', views.get_results, name='get_results'),
    path('proposal/', views.proposal, name='proposal'),
    path('approve/', views.approve_change, name='approve_change'),
    # New production API (no frontend build step)
    path('', include('optimizer.urls')),
]
