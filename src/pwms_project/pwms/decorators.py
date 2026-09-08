from functools import wraps

from django.shortcuts import render


# HTMX Partial Rendering Decorator
def htmx_partial(template_name):
    """
    Decorator that enables HTMX partial rendering for views.
    Returns template partial for HTMX requests, full template otherwise.
    """

    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            # Call the original view function
            response = view_func(request, *args, **kwargs)

            # If the view returned a response (not a dict), return it as-is
            if not isinstance(response, dict):
                return response

            # Handle HTMX partial rendering
            if request.headers.get("HX-Request"):
                # HTMX request - return only the content partial
                partial_template = f"{template_name}#content"
                return render(request, partial_template, response)
            else:
                # Regular request - return full page
                return render(request, template_name, response)

        return wrapper

    return decorator
