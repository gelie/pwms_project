"""Small template helpers shared across the PWMS pages."""

from django import template

from pwms.utils.documents import document_name as _document_name

register = template.Library()


@register.filter
def document_name(url):
    """The file name at the end of a document URL (``""`` when it has none).

    Templates pair it with their own fallback:

        {{ bill.bill_document_url|document_name|default:"Download" }}
    """
    return _document_name(url)
