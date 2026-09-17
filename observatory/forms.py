from django import forms

from . import survey
from .models import AirCastingDevice, Station
from .services import region


class StationLocationForm(forms.ModelForm):
    class Meta:
        model = Station
        fields = ["name", "region", "latitude", "longitude"]

    def clean(self):
        data = super().clean()
        lat, lon = data.get("latitude"), data.get("longitude")
        if (lat is None) != (lon is None):
            raise forms.ValidationError("Enter both latitude and longitude, or leave both empty.")
        if lat is not None and not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise forms.ValidationError("Latitude must be between -90 and 90 and longitude between -180 and 180.")
        # Placing a station by hand is the one way a point could reach the map from
        # outside the region, since every reading is checked on the way in.
        if lat is not None and region.enabled() and not region.contains(lat, lon):
            raise forms.ValidationError(
                f"That location is outside {region.name()}, which is the area this platform covers. "
                "Check the latitude and longitude, or leave both empty to leave the station unplaced.")
        return data


class DeviceForm(forms.ModelForm):
    class Meta:
        model = AirCastingDevice
        fields = ["device_id", "label", "known_session_ids", "project_tags", "download_from", "download_until",
                  "station", "active"]
        widgets = {"download_from": forms.DateInput(attrs={"type": "date"}),
                   "download_until": forms.DateInput(attrs={"type": "date"})}

    def clean_device_id(self):
        value = self.cleaned_data["device_id"].strip()
        if ":" not in value and "-" not in value:
            raise forms.ValidationError("Use the full identifier, e.g. AIRBEAM3:B0B21C7627C4.")
        # Checked here as well as on the model so that the field carries the error and
        # Django's own "already exists" message for the unique column is not added beside
        # it: two sentences saying the same thing is worse than one saying what to do.
        duplicate = AirCastingDevice.duplicate_message(value, exclude_pk=self.instance.pk)
        if duplicate:
            raise forms.ValidationError(duplicate)
        return value

    def clean(self):
        data = super().clean()
        start, end = data.get("download_from"), data.get("download_until")
        if start and end and end < start:
            raise forms.ValidationError("The end date must be on or after the start date.")
        return data


class SurveyForm(forms.Form):
    """Citizen feedback survey.

    Every rule the page enforces in JavaScript is enforced again here, because the form
    has to be completable without JavaScript — and because a browser is not a place to
    validate anything that matters.
    """
    consent = forms.BooleanField(
        required=True,
        error_messages={"required": "Please confirm you consent before submitting."},
        label="I have read the privacy information and I consent to my answers being used as described.")
    age_confirm = forms.BooleanField(
        required=True,
        error_messages={"required": "Please confirm you are 18 or over."},
        label="I am 18 or over.")

    county = forms.ChoiceField(
        choices=[("", "Select a county…")] + survey.COUNTY_CHOICES,
        error_messages={"required": "Please choose a county.",
                        "invalid_choice": "Please choose a county from the list."})
    area_type = forms.ChoiceField(
        choices=survey.AREA_TYPE, widget=forms.RadioSelect,
        error_messages={"required": "Please choose the kind of area you live in.",
                        "invalid_choice": "Please choose one of the options listed."})
    near_border = forms.BooleanField(required=False)

    air_quality_rating = forms.ChoiceField(
        choices=survey.AIR_QUALITY_RATING, widget=forms.RadioSelect,
        error_messages={"required": "Please describe the air quality where you live.",
                        "invalid_choice": "Please choose one of the options listed."})
    pollution_sources = forms.MultipleChoiceField(
        choices=survey.POLLUTION_SOURCES, required=False, widget=forms.CheckboxSelectMultiple)

    dashboard_usefulness = forms.ChoiceField(
        choices=survey.DASHBOARD_USEFULNESS, widget=forms.RadioSelect,
        error_messages={"required": "Please say how useful you find the dashboard.",
                        "invalid_choice": "Please choose one of the options listed."})
    dashboard_improvements = forms.MultipleChoiceField(
        choices=survey.DASHBOARD_IMPROVEMENTS, required=False, widget=forms.CheckboxSelectMultiple)
    dashboard_improvements_other = forms.CharField(
        required=False, max_length=survey.DASHBOARD_IMPROVEMENTS_OTHER_MAX)

    sensor_trust = forms.ChoiceField(
        choices=survey.SENSOR_TRUST, widget=forms.RadioSelect,
        error_messages={"required": "Please say how much you trust the sensor readings.",
                        "invalid_choice": "Please choose one of the options listed."})
    trust_concerns = forms.MultipleChoiceField(
        choices=survey.TRUST_CONCERNS, required=False, widget=forms.CheckboxSelectMultiple)

    participation = forms.MultipleChoiceField(
        choices=survey.PARTICIPATION, required=False, widget=forms.CheckboxSelectMultiple)
    open_data_support = forms.ChoiceField(
        choices=survey.OPEN_DATA_SUPPORT, widget=forms.RadioSelect,
        error_messages={"required": "Please say whether you agree that readings should be published openly.",
                        "invalid_choice": "Please choose one of the options listed."})

    additional_comments = forms.CharField(
        required=False, max_length=survey.ADDITIONAL_COMMENTS_MAX, widget=forms.Textarea)

    wants_results = forms.BooleanField(required=False)
    contact_email = forms.EmailField(
        required=False,
        error_messages={"invalid": "Please enter an email address in the form name@example.com."})

    @staticmethod
    def _apply_exclusive(selected, exclusive):
        """An "answers on its own" option wins: keep it and drop everything beside it."""
        chosen = set(selected)
        answered_alone = chosen & exclusive
        return sorted(answered_alone) if answered_alone else sorted(chosen)

    def clean_pollution_sources(self):
        return self._apply_exclusive(self.cleaned_data["pollution_sources"], survey.POLLUTION_SOURCES_EXCLUSIVE)

    def clean_trust_concerns(self):
        return self._apply_exclusive(self.cleaned_data["trust_concerns"], survey.TRUST_CONCERNS_EXCLUSIVE)

    def clean_participation(self):
        return self._apply_exclusive(self.cleaned_data["participation"], survey.PARTICIPATION_EXCLUSIVE)

    def clean_dashboard_improvements(self):
        chosen = self.cleaned_data["dashboard_improvements"]
        if len(chosen) > survey.DASHBOARD_IMPROVEMENTS_MAX:
            raise forms.ValidationError(f"Please choose up to {survey.DASHBOARD_IMPROVEMENTS_MAX}.")
        return sorted(chosen)

    def clean(self):
        data = super().clean()

        # "Something else" needs saying; anything typed against an unticked box is dropped.
        if "other" in (data.get("dashboard_improvements") or []):
            if not (data.get("dashboard_improvements_other") or "").strip():
                self.add_error("dashboard_improvements_other", "Please say what else would help.")
        else:
            data["dashboard_improvements_other"] = ""

        # The doubts question is only asked of people who expressed a doubt.
        if data.get("sensor_trust") not in survey.SENSOR_TRUST_WITH_DOUBTS:
            data["trust_concerns"] = []

        if data.get("wants_results"):
            if not data.get("contact_email"):
                self.add_error("contact_email", "Please enter an email address, or untick the box above.")
        else:
            data["contact_email"] = ""
        return data
