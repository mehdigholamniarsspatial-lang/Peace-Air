from django.contrib.auth.views import redirect_to_login
from django.urls import reverse


class LoginRequiredMiddleware:
    """Require authentication for the dashboard while leaving login and admin entry points available."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        public_paths = (reverse("login"), "/admin/login/")
        if not request.user.is_authenticated and request.path not in public_paths:
            return redirect_to_login(request.get_full_path(), reverse("login"))
        return self.get_response(request)
