from django.contrib.auth.views import LoginView
from corpus import admin_tools
from django.urls import path, include
from corpus import views, study_views
from corpus.reporting_views import coding_report

study_urls = [
    path("coding/", admin_tools.coding_workspace, name="admin-coding"),
    path("coding/api/state/", admin_tools.coding_state),
    path("coding/api/codebook/", admin_tools.coding_codebook),
    path(
        "coding/api/assignments/<int:assignment_id>/annotations/",
        admin_tools.coding_annotation,
    ),
    path(
        "coding/api/assignments/<int:assignment_id>/submit/",
        admin_tools.coding_complete,
    ),
    path(
        "coding/api/assignments/<int:assignment_id>/unsubmit/",
        admin_tools.coding_unsubmit,
    ),
    path("coding/codebook.yaml", admin_tools.coding_export, name="admin-coding-export"),
    path("codebook/", admin_tools.graph, name="study-codebook"),
    path("codebook/state/", admin_tools.graph_state, name="codebook-state"),
    path("codebook/publish/", admin_tools.graph_publish, name="codebook-publish"),
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
    path("logout/", admin_tools.logout, name="logout"),
    path("signed-out/", admin_tools.signed_out, name="signed-out"),
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
