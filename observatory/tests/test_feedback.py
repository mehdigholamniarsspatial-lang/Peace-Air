import html
import json
from pathlib import Path

from django.test import TestCase
from django.urls import reverse

from observatory import survey
from observatory.models import SurveyContact, SurveyResponse


def answers(**overrides):
    """A complete, valid submission; override one field to test that field."""
    data = {
        "consent": "on", "age_confirm": "on",
        "county": "Donegal", "area_type": "rural", "near_border": "on",
        "air_quality_rating": "good", "pollution_sources": ["home_heating", "agriculture"],
        "dashboard_usefulness": "fairly_useful", "dashboard_improvements": ["health_advice", "more_sensors"],
        "sensor_trust": "trust_somewhat", "participation": ["host_sensor", "attend_session"],
        "open_data_support": "agree", "additional_comments": "",
    }
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


class PublicAccessTests(TestCase):
    def test_the_survey_is_reachable_without_signing_in(self):
        response = self.client.get(reverse("feedback"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Air Quality Sensors and Data: Citizen Feedback Survey")

    def test_no_sign_in_is_needed_and_actions_stay_protected(self):
        """Viewing is public across this platform; only changes require a superuser."""
        self.assertEqual(self.client.get(reverse("feedback")).status_code, 200)

        blocked = self.client.post(reverse("device_add"), {"device_id": "AIRBEAM3:B0B21C7627C4"})
        self.assertEqual(blocked.status_code, 302)
        self.assertIn(reverse("login"), blocked.url)

    def test_the_survey_is_listed_in_the_navigation(self):
        self.assertContains(self.client.get(reverse("map")), reverse("feedback"))

    def test_the_page_makes_no_third_party_requests(self):
        body = self.client.get(reverse("feedback")).content.decode()

        for host in ("fonts.googleapis.com", "fonts.gstatic.com", "cdn.", "googleapis", "http://"):
            self.assertNotIn(host, body, host)
        # The only absolute link is the regulator's, and it is a link, not a request.
        self.assertEqual(body.count("https://"), 1)
        self.assertIn("https://www.dataprotection.ie", body)


class PrivacyNoticeTests(TestCase):
    def setUp(self):
        self.body = self.client.get(reverse("feedback")).content.decode()

    def test_it_names_the_controller_contact_and_retention(self):
        self.assertIn(survey.CONTROLLER_NAME, self.body)
        self.assertIn(survey.CONTACT_EMAIL, self.body)
        self.assertIn(survey.RETENTION_PERIOD, self.body)

    def test_there_is_no_data_protection_officer_line(self):
        self.assertNotIn("dpo", self.body.lower())
        self.assertNotIn("Data Protection Officer", self.body)

    def test_no_template_comment_leaks_into_the_page(self):
        """Django's {# #} is single-line; a multi-line one renders as visible text."""
        self.assertNotIn("{#", self.body)
        self.assertNotIn("#}", self.body)
        self.assertNotIn("{% comment", self.body)

    def test_the_notice_arrives_collapsed(self):
        self.assertIn('id="privacy"', self.body)
        self.assertNotIn('id="privacy" open', self.body)

    def test_the_summary_shows_that_it_opens(self):
        """Collapsed by default, so it has to look like something you can open."""
        self.assertIn("survey-privacy-chev", self.body)
        self.assertIn("survey-privacy-toggle", self.body)

        css = Path("observatory/static/observatory/css/app.css").read_text(encoding="utf-8")
        self.assertIn('.survey-privacy-toggle::after { content: "Show"; }', css)
        self.assertIn('.survey-privacy[open] .survey-privacy-toggle::after { content: "Hide"; }', css)
        self.assertIn(".survey-privacy[open] .survey-privacy-chev svg", css)

    def test_the_script_no_longer_decides_whether_it_is_open(self):
        source = Path("observatory/static/observatory/js/survey.js").read_text(encoding="utf-8")

        self.assertNotIn("privacy", source)

    def test_it_covers_every_heading_the_spec_requires(self):
        for heading in ("Who is collecting this", "Why", "Legal basis", "What we collect",
                        "What we don't collect", "Who sees it", "How long", "Your rights", "Complaints"):
            self.assertIn(heading, self.body)


class NoPersonalDataTests(TestCase):
    """The form must not ask for anything that identifies a respondent."""

    def test_no_field_asks_for_identifying_details(self):
        # Scoped to the form: the privacy notice mentions these to say we do NOT collect them.
        page = self.client.get(reverse("feedback")).content.decode().lower()
        form = page[page.index("<form"):page.index("</form>")]

        for banned in ("eircode", "postcode", "post code", "date of birth", "full address",
                       'name="name"', 'name="address"', 'name="dob"', 'name="age"', "ip address"):
            self.assertNotIn(banned, form, banned)

    def test_the_only_free_text_fields_are_the_two_the_spec_allows(self):
        page = self.client.get(reverse("feedback")).content.decode()
        form = page[page.index("<form"):page.index("</form>")]

        self.assertEqual(form.count("<textarea"), 1)
        self.assertEqual(form.count('type="text"'), 1)   # the "something else" box
        self.assertEqual(form.count('type="email"'), 1)  # the opt-in address

    def test_the_stored_answers_have_no_identifying_columns(self):
        columns = {f.name for f in SurveyResponse._meta.get_fields()}

        self.assertFalse(columns & {"name", "address", "postcode", "eircode", "ip_address", "date_of_birth"})
        self.assertNotIn("email", columns)


class ConsentGateTests(TestCase):
    def test_a_submission_without_consent_is_refused(self):
        response = self.client.post(reverse("feedback"), answers(consent=None))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(SurveyResponse.objects.exists())
        self.assertContains(response, "confirm you consent")

    def test_a_submission_without_the_age_confirmation_is_refused(self):
        response = self.client.post(reverse("feedback"), answers(age_confirm=None))

        self.assertFalse(SurveyResponse.objects.exists())
        self.assertContains(response, "confirm you are 18 or over")

    def test_the_consent_boxes_are_never_pre_ticked(self):
        body = self.client.get(reverse("feedback")).content.decode()
        start = body.index('id="id_consent"')

        self.assertNotIn("checked", body[start - 200:start + 200])


class RequiredQuestionTests(TestCase):
    def test_each_required_question_blocks_submission_on_its_own(self):
        for field, message in [
            ("county", "choose a county"),
            ("area_type", "kind of area"),
            ("air_quality_rating", "describe the air quality"),
            ("dashboard_usefulness", "how useful"),
            ("sensor_trust", "how much you trust"),
            ("open_data_support", "published openly"),
        ]:
            with self.subTest(field=field):
                response = self.client.post(reverse("feedback"), answers(**{field: None}))

                self.assertFalse(SurveyResponse.objects.exists())
                self.assertContains(response, message)

    def test_a_complete_submission_is_stored(self):
        response = self.client.post(reverse("feedback"), answers())

        self.assertContains(response, "Thank you.")
        stored = SurveyResponse.objects.get()
        self.assertEqual(stored.county, "Donegal")
        self.assertEqual(stored.area_type, "rural")
        self.assertTrue(stored.near_border)
        self.assertEqual(stored.pollution_sources, ["agriculture", "home_heating"])
        self.assertTrue(stored.consent_given)
        self.assertTrue(stored.age_confirmed)
        self.assertEqual(stored.schema_version, "1.0")


class ExclusivityTests(TestCase):
    def test_none_and_not_sure_answer_on_their_own_in_q2(self):
        self.client.post(reverse("feedback"), answers(pollution_sources=["home_heating", "none"]))

        self.assertEqual(SurveyResponse.objects.get().pollution_sources, ["none"])

    def test_no_concerns_answers_on_its_own_in_q4(self):
        self.client.post(reverse("feedback"), answers(
            sensor_trust="distrust_a_lot", trust_concerns=["accuracy", "no_concerns"]))

        self.assertEqual(SurveyResponse.objects.get().trust_concerns, ["no_concerns"])

    def test_not_interested_answers_on_its_own_in_q5(self):
        self.client.post(reverse("feedback"), answers(participation=["host_sensor", "not_interested"]))

        self.assertEqual(SurveyResponse.objects.get().participation, ["not_interested"])


class ConditionalFieldTests(TestCase):
    def test_improvements_are_capped_at_three(self):
        response = self.client.post(reverse("feedback"), answers(
            dashboard_improvements=["plain_language", "health_advice", "more_sensors", "alerts"]))

        self.assertFalse(SurveyResponse.objects.exists())
        self.assertContains(response, "Choose up to 3")

    def test_choosing_something_else_requires_saying_what(self):
        response = self.client.post(reverse("feedback"), answers(dashboard_improvements=["other"]))

        self.assertFalse(SurveyResponse.objects.exists())
        self.assertContains(response, "say what else would help")

    def test_a_rejected_other_answer_leaves_its_field_visible(self):
        """Without JavaScript the box must not hide the field the error asks you to fill."""
        response = self.client.post(reverse("feedback"), answers(dashboard_improvements=["other"]))
        body = response.content.decode()
        wrapper = body[body.index('id="wrap-improvements-other"'):][:60]

        self.assertNotIn("hidden", wrapper)

    def test_a_rejected_email_leaves_its_field_visible(self):
        response = self.client.post(reverse("feedback"), answers(wants_results="on", contact_email="nope"))
        body = response.content.decode()
        wrapper = body[body.index('id="wrap-contact-email"'):][:60]

        self.assertNotIn("hidden", wrapper)

    def test_text_typed_against_an_unticked_other_box_is_dropped(self):
        self.client.post(reverse("feedback"), answers(
            dashboard_improvements=["alerts"], dashboard_improvements_other="left over"))

        self.assertEqual(SurveyResponse.objects.get().dashboard_improvements_other, "")

    def test_concerns_are_discarded_when_no_doubt_was_expressed(self):
        self.client.post(reverse("feedback"), answers(
            sensor_trust="trust_a_lot", trust_concerns=["accuracy"]))

        self.assertEqual(SurveyResponse.objects.get().trust_concerns, [])

    def test_concerns_are_kept_when_a_doubt_was_expressed(self):
        self.client.post(reverse("feedback"), answers(
            sensor_trust="not_sure", trust_concerns=["accuracy", "validation"]))

        self.assertEqual(SurveyResponse.objects.get().trust_concerns, ["accuracy", "validation"])

    def test_a_comment_longer_than_the_limit_is_refused(self):
        response = self.client.post(reverse("feedback"), answers(additional_comments="x" * 1001))

        self.assertFalse(SurveyResponse.objects.exists())
        self.assertEqual(response.status_code, 200)


class ContactSeparationTests(TestCase):
    """The email and the answers must not be re-linkable."""

    def test_no_contact_row_exists_when_results_were_not_requested(self):
        self.client.post(reverse("feedback"), answers())

        self.assertEqual(SurveyContact.objects.count(), 0)
        self.assertTrue(SurveyResponse.objects.exists())

    def test_an_opted_in_email_is_stored_apart_from_the_answers(self):
        self.client.post(reverse("feedback"), answers(wants_results="on", contact_email="person@example.ie"))

        response = SurveyResponse.objects.get()
        contact = SurveyContact.objects.get()
        self.assertEqual(contact.email, "person@example.ie")
        # Nothing on either row points at the other.
        self.assertNotIn("email", {f.name for f in SurveyResponse._meta.get_fields()})
        self.assertFalse([f for f in SurveyContact._meta.get_fields() if f.is_relation])
        self.assertNotIn("person@example.ie", str(response.__dict__))

    def test_the_contact_keeps_a_date_not_a_submission_time(self):
        """A precise timestamp would pair a contact with the answers filed alongside it."""
        self.client.post(reverse("feedback"), answers(wants_results="on", contact_email="person@example.ie"))

        field = SurveyContact._meta.get_field("created_on")
        self.assertEqual(field.get_internal_type(), "DateField")

    def test_asking_for_results_without_an_email_is_refused(self):
        response = self.client.post(reverse("feedback"), answers(wants_results="on"))

        self.assertFalse(SurveyResponse.objects.exists())
        self.assertContains(response, "enter an email address")

    def test_a_malformed_email_is_refused(self):
        response = self.client.post(reverse("feedback"), answers(wants_results="on", contact_email="not-an-email"))

        self.assertFalse(SurveyResponse.objects.exists())
        self.assertContains(response, "name@example.com")


class JsonPayloadTests(TestCase):
    """The shape the page posts, exactly as the spec defines it."""

    PAYLOAD = {
        "schema_version": "1.0",
        "submitted_at": "2026-09-17T10:30:00Z",
        "consent": {"given": True, "age_confirmed": True, "consent_text_version": "1.0"},
        "answers": {
            "county": "Donegal", "area_type": "rural", "near_border": True,
            "air_quality_rating": "good", "pollution_sources": ["home_heating", "agriculture"],
            "dashboard_usefulness": "fairly_useful",
            "dashboard_improvements": ["health_advice", "more_sensors"],
            "dashboard_improvements_other": None,
            "sensor_trust": "trust_somewhat", "trust_concerns": [],
            "participation": ["host_sensor", "attend_session"],
            "open_data_support": "agree", "additional_comments": "",
        },
        "contact": {"wants_results": True, "email": "person@example.ie"},
    }

    def post(self, payload):
        return self.client.post(reverse("feedback"), data=json.dumps(payload),
                                content_type="application/json")

    def test_the_documented_payload_is_accepted(self):
        response = self.post(self.PAYLOAD)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(SurveyResponse.objects.get().county, "Donegal")
        self.assertEqual(SurveyContact.objects.get().email, "person@example.ie")

    def test_omitting_contact_entirely_stores_no_address(self):
        payload = {**self.PAYLOAD}
        payload.pop("contact")

        self.post(payload)

        self.assertTrue(SurveyResponse.objects.exists())
        self.assertEqual(SurveyContact.objects.count(), 0)

    def test_errors_come_back_per_field(self):
        payload = {**self.PAYLOAD, "answers": {**self.PAYLOAD["answers"], "county": ""}}

        response = self.post(payload)

        self.assertEqual(response.status_code, 400)
        self.assertIn("county", response.json()["errors"])
        self.assertFalse(SurveyResponse.objects.exists())

    def test_consent_is_enforced_on_the_json_path_too(self):
        payload = {**self.PAYLOAD, "consent": {"given": False, "age_confirmed": True}}

        response = self.post(payload)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(SurveyResponse.objects.exists())

    def test_unreadable_json_is_rejected_cleanly(self):
        response = self.client.post(reverse("feedback"), data="{not json",
                                    content_type="application/json")

        self.assertEqual(response.status_code, 400)


class AccessibilityMarkupTests(TestCase):
    def setUp(self):
        # Entity-escaped apostrophes are correct output; compare against the plain text.
        self.body = html.unescape(self.client.get(reverse("feedback")).content.decode())

    def test_every_radio_and_checkbox_group_has_a_fieldset_and_legend(self):
        self.assertEqual(self.body.count("<fieldset"), self.body.count("</fieldset>"))
        self.assertGreaterEqual(self.body.count("<legend>"), 10)

    def test_errors_are_announced_in_a_live_region(self):
        self.assertIn('aria-live="polite"', self.body)
        self.assertIn('id="form-status"', self.body)

    def test_the_free_text_warning_is_shown(self):
        self.assertIn(survey.FREE_TEXT_WARNING, self.body)

    def test_the_exact_question_wording_is_used(self):
        for label in [
            "Where are you answering from?",
            "This helps us compare views between areas. We only need a general location.",
            "How would you describe the air quality where you live?",
            "Which of these do you think affect the air in your area? (tick all that apply)",
            "How useful do you find the air quality dashboard?",
            "If you haven't used it before, choose the last option.",
            "What would make the information more useful to you? (choose up to 3)",
            "How much do you trust the readings from local low-cost sensors?",
            "If you have any doubts, what are they about?",
            "Would you take part in air quality monitoring yourself? (tick all that apply)",
            "Do you agree that sensor readings from your area should be published openly for anyone to see?",
        ]:
            self.assertIn(label, self.body, label)

    def test_the_prescribed_option_wording_is_used(self):
        for label in [
            "Home heating — turf, coal, wood or other solid fuel",
            "Pollution carried in from elsewhere, including across the border",
            "Whether readings are checked against official monitors",
            "Whether anything is actually done with the results",
            "Yes — I'd host a sensor at my home, school, workplace or community building",
        ]:
            self.assertIn(label, self.body, label)

    def test_all_thirty_two_counties_are_offered_in_two_groups(self):
        self.assertEqual(len(survey.COUNTIES_IRELAND), 26)
        self.assertEqual(len(survey.COUNTIES_NORTHERN_IRELAND), 6)
        self.assertIn('<optgroup label="Ireland">', self.body)
        self.assertIn('<optgroup label="Northern Ireland">', self.body)
        self.assertIn("Select a county…", self.body)
        self.assertIn("Prefer not to say", self.body)


class ProgressiveEnhancementTests(TestCase):
    def test_the_form_posts_without_javascript(self):
        body = self.client.get(reverse("feedback")).content.decode()

        self.assertIn('<form method="post"', body)
        self.assertIn(f'action="{reverse("feedback")}"', body)
        self.assertIn("csrfmiddlewaretoken", body)

    def test_the_script_only_enhances(self):
        source = Path("observatory/static/observatory/js/survey.js").read_text(encoding="utf-8")

        self.assertIn("sessionStorage", source)
        self.assertNotIn("localStorage", source)


class ReviewingResponsesTests(TestCase):
    """The supervisor reviews submissions through the Django admin, so it has to work."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        self.client.force_login(get_user_model().objects.create_superuser("boss", password="secret"))
        self.client.post(reverse("feedback"), answers(
            additional_comments="More sensors near the school please.",
            wants_results="on", contact_email="person@example.ie"))

    def test_responses_are_listed_in_the_admin(self):
        response = self.client.get(reverse("admin:observatory_surveyresponse_changelist"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Donegal")

    def test_contacts_are_listed_separately(self):
        response = self.client.get(reverse("admin:observatory_surveycontact_changelist"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "person@example.ie")
        # The answers must not be reachable from the address list.
        self.assertNotContains(response, "Donegal")

    def test_a_response_cannot_be_edited(self):
        listing = self.client.get(reverse("admin:observatory_surveyresponse_changelist"))

        self.assertNotContains(listing, "Add survey response")
        detail = self.client.get(reverse("admin:observatory_surveyresponse_change",
                                         args=[SurveyResponse.objects.get().pk]))
        self.assertNotContains(detail, 'name="_save"')

    def test_selected_responses_export_to_csv(self):
        response = self.client.post(reverse("admin:observatory_surveyresponse_changelist"), {
            "action": "export_responses",
            "_selected_action": [str(SurveyResponse.objects.get().pk)],
        })

        self.assertEqual(response["Content-Type"], "text/csv")
        body = response.content.decode()
        self.assertIn("county", body)
        self.assertIn("Donegal", body)
        self.assertIn("More sensors near the school please.", body)
        self.assertNotIn("person@example.ie", body)
