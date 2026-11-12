from django.test import TestCase, Client, override_settings
from django.urls import reverse
from .test_reporting import report_fixture
from .test_support import new_study
from .models import StudyAdmin, CodingSession
from .services import issue_link, delete_coder_session
from .private_links import private_link


@override_settings(CASE_EMAIL_ENABLED=False)
class PrivateLinkTests(TestCase):
    def setUp(self):
        self.data = report_fixture()
        self.study = self.data["study"]
        self.coder = self.data["alice"]
        self.url = reverse("dashboard", args=[self.study.pk])
        self.client.force_login(self.data["admin"])

    def reveal(self, client=None):
        return (client or self.client).post(
            self.url, {"action": "show_private_link", "session": self.coder.pk}
        )

    def test_admin_retrieves_same_link_without_rotating_or_exposing_it_on_dashboard(
        self,
    ):
        self.coder.refresh_from_db()
        token = self.data["link"].split("#")[1]
        self.assertNotIn(token, self.coder.encrypted_token)
        self.assertNotContains(self.client.get(self.url), token)
        self.assertContains(self.client.get(self.url), "Show private link")
        before = self.coder.token_hash
        response = self.reveal()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["url"], "http://testserver" + self.data["link"]
        )
        self.assertIn("no-store", response["Cache-Control"])
        self.coder.refresh_from_db()
        self.assertEqual(self.coder.token_hash, before)
        coder = Client()
        self.assertEqual(
            coder.post(
                "/access/exchange/", {"token": token}, content_type="application/json"
            ).status_code,
            200,
        )

    def test_study_admin_access_and_cross_study_and_coder_denial(self):
        other = new_study()
        self.client.force_login(other.owner)
        self.assertEqual(self.reveal().status_code, 404)
        member = StudyAdmin.objects.create(
            study=self.study, user=other.owner, granted_by=self.study.owner
        )
        self.assertEqual(self.reveal().status_code, 200)
        member.delete()
        self.assertEqual(self.reveal().status_code, 404)
        coder = Client()
        coder.post(
            "/access/exchange/",
            {"token": self.data["link"].split("#")[1]},
            content_type="application/json",
        )
        self.assertEqual(self.reveal(coder).status_code, 302)
        secure = Client(enforce_csrf_checks=True)
        secure.force_login(self.data["admin"])
        self.assertEqual(self.reveal(secure).status_code, 403)

    def test_rotation_revocation_and_deletion(self):
        new = issue_link(self.coder)
        self.assertEqual(self.reveal().json()["url"], "http://testserver" + new)
        self.assertNotEqual(new, self.data["link"])
        self.client.post(self.url, {"action": "revoke", "session": self.coder.pk})
        self.coder.refresh_from_db()
        self.assertEqual(self.coder.encrypted_token, "")
        self.assertEqual(self.reveal().status_code, 409)
        issue_link(self.coder)
        delete_coder_session(self.coder.pk)
        self.coder.refresh_from_db()
        self.assertEqual(self.coder.encrypted_token, "")
        self.assertEqual(self.reveal().status_code, 404)

    def test_missing_or_invalid_ciphertext_never_rotates_or_leaks(self):
        for encrypted in ["", "invalid", self.data["bob"].encrypted_token]:
            CodingSession.objects.filter(pk=self.coder.pk).update(
                encrypted_token=encrypted
            )
            response = self.reveal()
            self.assertEqual(response.status_code, 409)
            self.assertNotIn(self.data["link"].split("#")[1], response.content.decode())
            self.coder.refresh_from_db()
            self.assertTrue(self.coder.token_hash)
        issue_link(self.coder)
        with override_settings(SECRET_KEY="different-key"):
            from .services import Conflict

            with self.assertRaises(Conflict):
                private_link(self.coder)
