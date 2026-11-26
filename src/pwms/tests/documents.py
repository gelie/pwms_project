from django.test import SimpleTestCase

from ..utils.documents import document_name


class DocumentNameTests(SimpleTestCase):
    """The readable file name used as a document link's label."""

    def test_the_last_path_segment_is_decoded(self):
        self.assertEqual(
            document_name(
                "https://contoso.sharepoint.com/sites/pwms/Shared%20Documents/"
                "Bill%20Report.pdf?download=1"
            ),
            "Bill Report.pdf",
        )

    def test_a_url_without_a_file_name_returns_empty(self):
        for url in (
            "",
            "https://contoso.sharepoint.com/sites/pwms/",
            "https://contoso.sharepoint.com/download.aspx?UniqueId=1",
        ):
            with self.subTest(url=url):
                self.assertEqual(document_name(url), "")
