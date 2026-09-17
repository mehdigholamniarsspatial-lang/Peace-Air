from django.test import TestCase
from django.urls import reverse


class NavigationTests(TestCase):
    """About is the front door; analysis and settings folded into the other two pages."""

    def test_root_opens_the_about_page(self):
        self.assertRedirects(self.client.get(reverse("home")), reverse("about"), fetch_redirect_response=False)

    def test_the_logo_links_to_about(self):
        response = self.client.get(reverse("map"))
        self.assertContains(response, f'class="brand" href="{reverse("about")}"')

    def test_analysis_and_settings_are_not_separate_sections(self):
        response = self.client.get(reverse("map"))
        self.assertNotContains(response, ">Station analysis</a>")
        self.assertNotContains(response, ">Settings</a>")
        self.assertNotContains(response, ">Overview</a>")

    def test_the_old_analysis_link_lands_on_the_map_with_its_station(self):
        response = self.client.get(reverse("analysis"), {"station": "GW-014", "measurement": "PM10"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("map"), response.url)
        self.assertIn("station=GW-014", response.url)
        self.assertIn("measurement=PM10", response.url)

    def test_the_old_settings_link_lands_on_the_data_manager(self):
        self.assertRedirects(self.client.get(reverse("settings")), reverse("data"), fetch_redirect_response=False)
