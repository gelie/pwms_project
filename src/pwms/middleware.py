"""Project-wide middleware."""

from django.contrib.auth.middleware import LoginRequiredMiddleware


class SiteLoginRequiredMiddleware(LoginRequiredMiddleware):
    """
    Require authentication for every site route by default.

    Views marked with ``django.contrib.auth.decorators.login_not_required``
    (the home page and Django's own auth views) stay reachable anonymously;
    everything else redirects anonymous visitors to ``settings.LOGIN_URL``
    with a ``?next=`` pointing back at the page they asked for.

    Namespaces that implement their own authentication are skipped, so this
    middleware never hijacks their login/redirect flow or turns an API response
    into an HTML redirect:

    * ``admin`` - the Django admin has its own login page and staff checks.
    * ``rest_framework`` / ``pwms_api`` / ``pwms_ninja`` - API namespaces answer
      anonymous clients with 401/403 rather than a 302 to the site login. (DRF
      already opts its own views out; this covers the browsable-API auth pages
      and django-ninja.)
    """

    exempt_namespaces = frozenset({"admin", "rest_framework", "pwms_api", "pwms_ninja"})

    def process_view(self, request, view_func, view_args, view_kwargs):
        match = request.resolver_match
        if match and match.namespace.split(":")[-1] in self.exempt_namespaces:
            return None
        return super().process_view(request, view_func, view_args, view_kwargs)
