from django.contrib import admin
from django.urls import path
from corpus import views

urlpatterns = [
    path("healthz/", views.health),
    path("admin/", admin.site.urls),
    path("", views.dashboard, name="dashboard"),
    path("access/", views.access),
    path("access/exchange/", views.exchange),
    path("work/", views.workspace),
    path("api/state/", views.state),
    path("api/assignments/<int:assignment_id>/annotations/", views.annotation),
    path("api/assignments/<int:assignment_id>/submit/", views.complete),
    path("api/codebook/", views.codebook),
    path("work/codebook.yaml", views.personal_export),
    path("review/<int:submission_id>/", views.review, name="review"),
    path("exports/shared.yaml", views.shared_export),
    path(
        "exports/submissions/<int:submission_id>/", views.results_export, name="results"
    ),
]
