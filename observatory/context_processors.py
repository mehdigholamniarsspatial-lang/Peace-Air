from django.conf import settings

from .models import ImportRun


def platform(request):
    demo = ImportRun.objects.filter(source="demo").exists() and not ImportRun.objects.exclude(source="demo").exists()
    return {"is_demo_data": demo, "tile_url": settings.OBSERVATORY_TILE_URL,
            "tile_attribution": settings.OBSERVATORY_TILE_ATTRIBUTION}
