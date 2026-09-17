from pathlib import Path

from django.test import TestCase
from django.contrib.auth import get_user_model
from django.urls import reverse

from observatory.models import Station


class DataManagerPageTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("viewer", password="t"))

    def test_default_region_is_ireland_and_region_options_are_distinct(self):
        Station.objects.create(code="A", name="Alpha", region="Ireland")
        Station.objects.create(code="B", name="Beta", region="Ireland")

        response = self.client.get(reverse("data"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<option value="Ireland" selected>Region: Ireland</option>', html=True)
        self.assertEqual(list(response.context["regions"]), ["Ireland"])


class MapExplorerLayoutTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("mapviewer"))

    def test_the_distribution_sits_under_the_time_series(self):
        body = self.client.get(reverse("map")).content.decode()

        self.assertIn('id="series"', body)
        self.assertIn('id="hist"', body)
        self.assertLess(body.index('id="series"'), body.index('id="hist"'))

    def test_the_period_control_sits_above_the_series_with_dates_on_top(self):
        body = self.client.get(reverse("map")).content.decode()

        self.assertIn('class="time-strip"', body)
        self.assertLess(body.index('id="start"'), body.index('id="window-slider"'))
        self.assertLess(body.index('id="window-slider"'), body.index('id="series"'))

    def test_the_read_only_date_range_box_is_gone(self):
        response = self.client.get(reverse("map"))

        self.assertNotContains(response, 'id="date-summary"')
        self.assertNotContains(response, ">Date range<")

    def test_there_is_only_one_period_slider(self):
        body = self.client.get(reverse("map")).content.decode()

        self.assertEqual(body.count('id="window-slider"'), 1)
        self.assertNotIn('id="series-slider"', body)

    def test_the_map_explorer_carries_the_analysis_controls(self):
        response = self.client.get(reverse("map"))

        for element in ('id="aggregation"', 'id="s-mean"', 'id="s-std"', 'id="play"', 'id="download-chart"'):
            self.assertContains(response, element)

    def test_the_observation_range_control_is_gone(self):
        response = self.client.get(reverse("map"))

        self.assertNotContains(response, 'id="obs"')
        self.assertNotContains(response, "Observation range")

    def test_the_statistics_say_what_they_describe(self):
        """Aggregation reshapes the plot but not these figures, so the page has to say so."""
        response = self.client.get(reverse("map"))

        self.assertContains(response, 'id="stats-note"')

    def test_the_map_popup_has_no_link_to_a_removed_page(self):
        source = (Path("observatory/static/observatory/js") / "map.js").read_text(encoding="utf-8")

        self.assertNotIn("pop-link", source)
        self.assertNotIn("/analysis/?station=", source)


class TemplateCommentTests(TestCase):
    """``{# … #}`` cannot span lines: Django renders the rest of it as page text.

    The failure is silent — the template still compiles, and a note meant for whoever
    reads the file next turns up in the middle of the interface. Checked against the
    source rather than one rendered page so a template nobody thought to test is covered
    too.
    """

    def test_no_template_comment_spans_more_than_one_line(self):
        root = Path(__file__).resolve().parents[1] / "templates"
        offenders = []
        for template in sorted(root.rglob("*.html")):
            for number, line in enumerate(template.read_text(encoding="utf-8").splitlines(), start=1):
                if "{#" in line and "#}" not in line.split("{#", 1)[1]:
                    offenders.append(f"{template.relative_to(root)}:{number}")
        self.assertEqual(offenders, [], "Use {% comment %} … {% endcomment %} for these")
