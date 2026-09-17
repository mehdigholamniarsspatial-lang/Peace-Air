from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class AuthenticationTests(TestCase):
    def test_dashboard_is_public(self):
        response = self.client.get(reverse("map"))
        self.assertEqual(response.status_code, 200)

    def test_public_api_is_available_without_login(self):
        self.assertEqual(self.client.get(reverse("api_stations")).status_code, 200)

    def test_mutating_api_requires_login(self):
        response = self.client.post(
            reverse("api_datasets_delete"), data={"ids": [1]}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 403)

    def test_public_data_page_points_protected_actions_to_login(self):
        response = self.client.get(reverse("data"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'data-login-url="{reverse("login")}?next={reverse("data")}"')

    def test_superuser_can_sign_in_and_view_reports(self):
        get_user_model().objects.create_superuser("admin", password="secret")
        self.assertTrue(self.client.login(username="admin", password="secret"))
        self.assertEqual(self.client.get(reverse("reports")).status_code, 200)

    def test_regular_user_cannot_change_schedule(self):
        self.client.force_login(get_user_model().objects.create_user("viewer"))
        response = self.client.post(reverse("api_schedule"), data={"enabled": True},
                                    content_type="application/json")
        self.assertEqual(response.status_code, 403)

    def test_regular_user_cannot_add_a_sensor(self):
        self.client.force_login(get_user_model().objects.create_user("viewer"))
        response = self.client.post(reverse("device_add"), {"device_id": "AIRBEAM3:B0B21C7627C4"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)
