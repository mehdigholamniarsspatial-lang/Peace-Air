from django.test import TestCase
from django.contrib.auth import get_user_model
from django.urls import reverse


class AboutPageTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("viewer"))

    def test_about_route_renders_heading_and_wp2_section(self):
        response = self.client.get(reverse("about"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<h1>About This Platform</h1>", html=True)
        self.assertContains(response, "Work Package 2: Transport-Related Air Pollution")
        self.assertContains(response, "Dr Liz Coleman")
        self.assertContains(response, "liz.coleman@universityofgalway.ie")
        self.assertContains(response, 'href="https://research.universityofgalway.ie/en/persons/liz-coleman/"')
        self.assertNotContains(response, "PEACEPLUS / SEUPB logos")
        self.assertNotContains(response, "TODO:")
