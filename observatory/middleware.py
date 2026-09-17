from django.contrib.auth.views import redirect_to_login
from django.urls import reverse


class LoginRequiredMiddleware:
    """Require authentication for the dashboard.

    The login and admin entry points stay open, and so does the citizen feedback survey:
    it is addressed to the public, and asking people to hold an account before telling us
    about the air where they live would defeat it.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        public_paths = (reverse("login"), reverse("feedback"), "/admin/login/")
        if not request.user.is_authenticated and request.path not in public_paths:
            return redirect_to_login(request.get_full_path(), reverse("login"))
        return self.get_response(request)
