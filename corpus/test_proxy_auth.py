from .test_support import new_study
from .studies import publish_codebook
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from .models import SharedCodebook
from .services import create_session


@override_settings(
    AUTHELIA_AUTH_ENABLED=True,
    AUTHELIA_PROXY_SECRET="test-proxy-secret-with-at-least-32-characters",
    AUTHELIA_TRUSTED_PROXIES=["192.168.1.60/32"],
    AUTHELIA_ADMIN_GROUP="coding-admins",
)
class ProxyAuthenticationTests(TestCase):

    def headers(self, **overrides):
        headers = dict(
            REMOTE_ADDR="192.168.1.60",
            HTTP_X_CODING_PROXY_SECRET="test-proxy-secret-with-at-least-32-characters",
            HTTP_REMOTE_USER="researcher",
            HTTP_REMOTE_GROUPS="home, coding-admins",
        )
        headers.update(overrides)
        return headers

    def test_group_member_receives_staff_access(self):
        self.assertEqual(self.client.get("/", **self.headers()).status_code, 200)
        user = get_user_model().objects.get(username="authelia:researcher")
        self.assertTrue(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.has_usable_password())

    def test_admin_entry_opens_study_list(self):
        response = self.client.get("/", **self.headers())
        self.assertContains(response, "My studies")
        self.assertContains(response, "Create a study")

    def test_nonmembers_and_missing_headers_are_denied(self):
        self.assertEqual(self.client.get("/").status_code, 403)
        self.assertEqual(
            self.client.get("/", **self.headers(HTTP_REMOTE_GROUPS="home")).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                "/", **self.headers(HTTP_REMOTE_GROUPS="coding-admins-other")
            ).status_code,
            403,
        )

    def test_proxy_identity_cannot_be_spoofed(self):
        self.assertEqual(
            self.client.get(
                "/",
                **self.headers(
                    REMOTE_ADDR="192.168.1.99", HTTP_X_FORWARDED_FOR="192.168.1.60"
                )
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                "/", **self.headers(HTTP_X_CODING_PROXY_SECRET="wrong")
            ).status_code,
            403,
        )

    def test_password_cookie_cannot_bypass_group_check(self):
        user = get_user_model().objects.create_superuser(
            "local", "admin@example.org", "password"
        )
        self.client.force_login(user)
        self.assertEqual(self.client.get("/").status_code, 403)
        self.assertEqual(
            self.client.post(
                "/admin/login/", {"username": "local", "password": "password"}
            ).status_code,
            403,
        )

    def test_group_removal_takes_effect_on_next_request(self):
        self.assertEqual(self.client.get("/", **self.headers()).status_code, 200)
        self.assertEqual(
            self.client.get("/", **self.headers(HTTP_REMOTE_GROUPS="home")).status_code,
            403,
        )
        get_user_model().objects.filter(username="authelia:researcher").update(
            is_active=False
        )
        self.assertEqual(self.client.get("/", **self.headers()).status_code, 403)

    def test_private_coder_links_remain_independent(self):
        self.study = new_study()
        publish_codebook(
            self.study,
            book={"version": 1, "codes": []},
            expected_revision=(self.study)
            .__class__.objects.get(pk=(self.study).pk)
            .codebook_id,
        )
        session, link = create_session(self.study, "Coder", [])
        response = self.client.post(
            "/access/exchange/",
            {"token": link.split("#")[1]},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/state/").json()["coder"], session.coder)
        self.assertEqual(self.client.get("/work/", **self.headers()).status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 403)

    def test_health_endpoint_is_available(self):
        self.assertEqual(self.client.get("/healthz/").status_code, 200)

    def test_app_logout_keeps_sso_and_public_landing(self):
        self.client.get("/", **self.headers())
        self.client.post("/logout/")
        landing = self.client.get("/signed-out/")
        self.assertContains(landing, "Authelia remains signed in")
        self.assertIn("no-store", landing["Cache-Control"])
        self.assertEqual(self.client.get("/", **self.headers()).status_code, 200)
        denied = self.client.get("/", **self.headers(HTTP_REMOTE_GROUPS="home"))
        self.assertEqual(denied.status_code, 403)
        self.assertIn("no-store", denied["Cache-Control"])
