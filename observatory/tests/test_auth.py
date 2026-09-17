import re

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from observatory.models import AirCastingDevice, Station


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

    def test_the_administrative_pages_are_not_reachable_by_url(self):
        """Hiding the menu is not enough: the pages themselves are closed."""
        for page in ("data", "reports"):
            with self.subTest(page=page):
                response = self.client.get(reverse(page))
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("login"), response.url)

    def test_a_signed_in_non_administrator_cannot_reach_them_either(self):
        self.client.force_login(get_user_model().objects.create_user("viewer"))

        for page in ("data", "reports"):
            with self.subTest(page=page):
                self.assertEqual(self.client.get(reverse(page)).status_code, 302)

    def test_the_json_behind_those_pages_is_closed_too(self):
        """Sensor identifiers and the import log must not stay readable as JSON."""
        for endpoint in ("api_devices", "api_imports"):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.client.get(reverse(endpoint)).status_code, 403)

    def test_the_public_pages_stay_open(self):
        for page in ("about", "map", "feedback"):
            with self.subTest(page=page):
                self.assertEqual(self.client.get(reverse(page)).status_code, 200)

    def test_an_administrator_can_open_both_pages(self):
        self.client.force_login(get_user_model().objects.create_superuser("boss", password="secret"))

        for page in ("data", "reports"):
            with self.subTest(page=page):
                self.assertEqual(self.client.get(reverse(page)).status_code, 200)

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


class LandingAfterSignInTests(TestCase):
    """The data manager is an administrator's page: it is where they land, and it is in
    the menu only for them."""

    def sign_in(self, **kwargs):
        users = get_user_model().objects
        maker = users.create_superuser if kwargs.pop("admin", False) else users.create_user
        maker(kwargs["username"], password="secret")
        return self.client.post(reverse("login"), {"username": kwargs["username"], "password": "secret"})

    def test_an_administrator_lands_on_the_data_manager(self):
        response = self.sign_in(username="admin", admin=True)

        self.assertRedirects(response, reverse("after_login"), target_status_code=302)
        self.assertRedirects(self.client.get(reverse("after_login")), reverse("data"))

    def test_anyone_else_lands_on_the_map(self):
        self.sign_in(username="viewer")

        self.assertRedirects(self.client.get(reverse("after_login")), reverse("map"))

    def test_the_data_manager_is_in_the_menu_for_an_administrator(self):
        self.client.force_login(get_user_model().objects.create_superuser("admin", password="secret"))

        self.assertContains(self.client.get(reverse("map")), f'href="{reverse("data")}"')

    def test_the_data_manager_is_not_in_the_menu_for_a_regular_user(self):
        self.client.force_login(get_user_model().objects.create_user("viewer"))

        response = self.client.get(reverse("map"))
        self.assertNotContains(response, ">Data manager</a>")
        self.assertContains(response, ">Map explorer</a>")

    def test_the_data_manager_is_not_in_the_menu_for_a_signed_out_visitor(self):
        response = self.client.get(reverse("map"))

        self.assertNotContains(response, ">Data manager</a>")

    def test_reports_is_in_the_menu_only_for_an_administrator(self):
        self.assertNotContains(self.client.get(reverse("map")), ">Reports</a>")

        self.client.force_login(get_user_model().objects.create_superuser("boss", password="secret"))
        self.assertContains(self.client.get(reverse("map")), ">Reports</a>")

    def test_every_inline_handler_stays_on_one_line(self):
        """A line break inside onsubmit is a JavaScript syntax error: the handler never
        runs, and the form then submits with no confirmation at all."""
        self.client.force_login(get_user_model().objects.create_superuser("boss", password="secret"))
        # The confirm handlers live on the device and station rows, so there must be one of each.
        station = Station.objects.create(code="S1", name="Site one")
        AirCastingDevice.objects.create(device_id="AIRBEAM3:B0B21C7627C4", station=station)
        body = self.client.get(reverse("data")).content.decode()

        unbroken = re.findall(r'onsubmit="[^"\n]*"', body)
        self.assertEqual(len(unbroken), body.count('onsubmit="'))
        self.assertGreater(len(unbroken), 0)


class AdminPanelLinkTests(TestCase):
    """The Django admin holds the survey responses, so an administrator needs a way in."""

    def test_an_administrator_sees_the_admin_panel_link(self):
        self.client.force_login(get_user_model().objects.create_superuser("boss", password="secret"))

        response = self.client.get(reverse("map"))

        self.assertContains(response, f'href="{reverse("admin:index")}"')
        self.assertContains(response, ">Admin panel</a>")

    def test_a_regular_user_does_not(self):
        self.client.force_login(get_user_model().objects.create_user("viewer", password="secret"))

        self.assertNotContains(self.client.get(reverse("map")), ">Admin panel</a>")

    def test_a_signed_out_visitor_does_not(self):
        self.assertNotContains(self.client.get(reverse("map")), ">Admin panel</a>")

    def test_the_link_reaches_the_admin_for_an_administrator(self):
        self.client.force_login(get_user_model().objects.create_superuser("boss", password="secret"))

        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)
