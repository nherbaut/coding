from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path, include
from corpus import views, study_views
from corpus.reporting_views import coding_report

study_urls = [
    path("", views.dashboard, name="dashboard"),
    path("settings/", study_views.study_settings, name="study-settings"),
    path("protocol/", study_views.study_protocol, name="study-protocol"),
    path("corpus/", study_views.study_corpus, name="study-corpus"),
    path("archive/", views.coding_archive, name="coding-archive"),
    path("reports/", coding_report, name="coding-report"),
    path("reports/<uuid:report_id>/", coding_report, name="saved-report"),
    path("reviews/<int:submission_id>/", views.review, name="review"),
    path("codebook.yaml", views.shared_export, name="shared-export"),
    path(
        "exports/submissions/<int:submission_id>/", views.results_export, name="results"
    ),
]
urlpatterns = [
    path("healthz/", views.health),
    path("", study_views.study_list, name="study-list"),
    path("admin/studies/<uuid:study_id>/", include(study_urls)),
    path(
        "admin/login/",
        LoginView.as_view(
            template_name="corpus/login.html", redirect_authenticated_user=True
        ),
        name="login",
    ),
    path("admin/logout/", LogoutView.as_view(next_page="/"), name="logout"),
    path("access/", views.access),
    path("access/exchange/", views.exchange),
    path("work/", views.workspace),
    path("api/state/", views.state),
    path("api/assignments/<int:assignment_id>/annotations/", views.annotation),
    path("api/assignments/<int:assignment_id>/submit/", views.complete),
    path("api/assignments/<int:assignment_id>/unsubmit/", views.unsubmit),
    path("api/codebook/", views.codebook),
    path("work/codebook.yaml", views.personal_export),
]
