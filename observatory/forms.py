from django import forms

from .models import AirCastingDevice, Station


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
        return value

    def clean(self):
        data = super().clean()
        start, end = data.get("download_from"), data.get("download_until")
        if start and end and end < start:
            raise forms.ValidationError("The end date must be on or after the start date.")
        return data
