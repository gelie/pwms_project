"""Tests for the per-bill SharePoint folder service (``bill_create`` filing).

Graph is never called for real: :mod:`pwms.utils.sharepoint`'s async helpers are
replaced with mock transports/``AsyncMock``s and the application token with a
stub, so the suite runs offline the way :mod:`pwms.tests_attachments` does.
"""

import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import httpx
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from . import views
from .models import (
    Attachment,
    Bill,
    Group,
    GroupMembership,
    Role,
    SharepointDrive,
    SharepointFolder,
    SharepointSite,
    SharepointSiteMember,
    WorkflowType,
)
from .services import attachments as attachments_service
from .services import bill_folders
from .services.bill_folders import BillFolderError, ensure_bill_folder
from .utils import sharepoint as graph

# The ``SimpleTestCase`` classes exercise the Graph plumbing off the ORM, so
# they run without a database; every ``_BillFixtureTestCase`` subclass drives
# real views/rows and needs it.

TOKEN = {"access_token": "app-token", "expires_in": 3600}
ITEM = {"id": "child-item", "name": "B 99—2026", "folder": {}, "webUrl": "https://x"}

#: A Graph driveItem payload for an uploaded file (fixture for the picker).
_GRAPH_FILE = {
    "id": "item-9",
    "name": "minutes.pdf",
    "size": 2048,
    "file": {"mimeType": "application/pdf"},
    "webUrl": "https://contoso.sharepoint.com/minutes.pdf",
}

#: The endpoint the filing contract names, as assembled from the settings ids.
FOLDER_URL = "https://graph.microsoft.com/v1.0/drives/drive-1/items/parent-1/children"


@contextmanager
def _offline_graph(handler):
    """Point ``create_child_folder``'s httpx calls at a mock transport."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    with (
        patch.object(graph, "get_application_token", return_value=TOKEN),
        patch.object(
            graph.httpx,
            "AsyncClient",
            lambda **kwargs: real_client(transport=transport, **kwargs),
        ),
    ):
        yield


class FolderNameTests(SimpleTestCase):
    """The SharePoint-safe rendering of a B-number."""

    def test_illegal_characters_fold_to_a_dash(self):
        self.assertEqual(
            bill_folders.folder_name_for("B.74-2/2025 (AS)"), "B.74-2-2025 (AS)"
        )
        self.assertEqual(
            bill_folders.folder_name_for('a/b\\c:d"e|f*g?h<i>'),
            "a-b-c-d-e-f-g-h-i-",
        )

    def test_trailing_dots_and_spaces_are_dropped(self):
        self.assertEqual(bill_folders.folder_name_for("B 99—2026  ."), "B 99—2026")

    def test_names_never_exceed_sharepoints_limit(self):
        self.assertEqual(len(bill_folders.folder_name_for("B " + "x" * 400)), 255)


@override_settings(BILL_DRIVE="drive-1", BILL_FOLDER="parent-1")
class EnsureBillFolderTests(SimpleTestCase):
    """``ensure_bill_folder`` against a mocked Graph client."""

    def test_a_missing_folder_is_created(self):
        calls = []

        async def handler(request):
            calls.append(request)
            return httpx.Response(
                201, json={"id": "new-item", "name": "B 99—2026", "folder": {}}
            )

        with _offline_graph(handler):
            status = ensure_bill_folder("B 99—2026")

        self.assertEqual(status, bill_folders.CREATED)
        (request,) = calls
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), FOLDER_URL)
        self.assertEqual(
            json.loads(request.read()),
            {"name": "B 99—2026", "folder": {}},
        )

    def test_an_existing_folder_is_reused(self):
        async def handler(request):
            if request.method == "POST":
                return httpx.Response(
                    409,
                    json={"error": {"code": "nameAlreadyExists", "message": "taken"}},
                )
            return httpx.Response(200, json={"value": [ITEM]})

        with _offline_graph(handler):
            status = ensure_bill_folder("B 99—2026")

        self.assertEqual(status, bill_folders.ALREADY_THERE)

    def test_the_existing_child_is_found_across_pages(self):
        async def handler(request):
            if request.method == "POST":
                return httpx.Response(
                    409,
                    json={"error": {"code": "nameAlreadyExists", "message": "taken"}},
                )
            if "page" not in request.url.params:
                return httpx.Response(
                    200,
                    json={
                        "value": [{"id": "not-it", "name": "B 1", "folder": {}}],
                        "@odata.nextLink": FOLDER_URL + "?page=2",
                    },
                )
            return httpx.Response(200, json={"value": [ITEM]})

        with _offline_graph(handler):
            status = ensure_bill_folder("B 99—2026")

        self.assertEqual(status, bill_folders.ALREADY_THERE)

    def test_a_file_wearing_the_name_is_an_error(self):
        async def handler(request):
            if request.method == "POST":
                return httpx.Response(
                    409,
                    json={"error": {"code": "nameAlreadyExists", "message": "taken"}},
                )
            return httpx.Response(
                200, json={"value": [{"id": "file", "name": ITEM["name"]}]}
            )

        with _offline_graph(handler), self.assertRaises(BillFolderError):
            ensure_bill_folder("B 99—2026")

    def test_graph_failures_raise_bill_folder_errors(self):
        with (
            self.assertRaises(BillFolderError),
            _offline_graph(lambda request: httpx.Response(503, text="nope")),
        ):
            ensure_bill_folder("B 99—2026")


class EnsureBillFolderConfigTests(SimpleTestCase):
    """Configuration gates, which must not touch the network."""

    @override_settings(BILL_DRIVE="", BILL_FOLDER="")
    def test_an_unconfigured_deployment_skips_sharepoint(self):
        with (
            patch.object(graph, "get_application_token") as token,
            patch.object(graph, "create_child_folder") as create,
        ):
            status = ensure_bill_folder("B 99—2026")

        self.assertEqual(status, bill_folders.UNCONFIGURED)
        token.assert_not_called()
        create.assert_not_called()

    @override_settings(BILL_DRIVE="drive-1", BILL_FOLDER="parent-1")
    def test_a_blank_bill_number_is_rejected_before_graph_is_called(self):
        with (
            patch.object(graph, "get_application_token") as token,
            patch.object(graph, "create_child_folder") as create,
            self.assertRaises(BillFolderError),
        ):
            ensure_bill_folder("   ")

        token.assert_not_called()
        create.assert_not_called()


class BillFolderPreviewTests(SimpleTestCase):
    """The HTMX hint under the form's B-number field.

    Called through the ``RequestFactory`` (the login middleware is what makes
    the real route authenticated, and it does not run here).
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.rf = RequestFactory()

    @override_settings(BILL_DRIVE="drive-1", BILL_FOLDER="parent-1")
    def test_the_preview_shows_the_folder_a_number_will_file_into(self):
        request = self.rf.get("/bills/folder-preview", {"bill_number": "B.2/2025 (AS)"})
        request.user = AnonymousUser()  # What SiteLoginRequiredMiddleware supplies.
        response = views.bill_folder_preview(request)

        self.assertContains(response, "Bill Folder / B.2-2025 (AS)")

    @override_settings(BILL_DRIVE="", BILL_FOLDER="")
    def test_an_unconfigured_deployment_says_so(self):
        request = self.rf.get("/bills/folder-preview", {"bill_number": "B.2/2025"})
        request.user = AnonymousUser()
        response = views.bill_folder_preview(request)

        self.assertContains(response, "not configured")


class _BillFixtureTestCase(TestCase):
    """A creator wired to the Bill type and the bill drive's local mirror rows.

    Shared by the filing-flow tests: the attachment machinery resolves folders
    through ``SharepointDrive`` rows, so ``drive-1`` exists locally even though
    every Graph call itself is mocked.
    """

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(
            username="bill-filer", email="filer@example.com", password="pw"
        )
        cls.group = Group.objects.create(
            name="Bill Filing Group", group_type="portfolio_committee"
        )
        cls.creator_role = Role.objects.create(name="Bill Filing Creator")
        GroupMembership.objects.create(
            user=cls.user, group=cls.group, role=cls.creator_role
        )
        cls.bill_type = WorkflowType.objects.get(name="Bill")
        cls.bill_type.group = cls.group
        cls.bill_type.save(update_fields=["group"])
        cls.bill_type.create_roles.add(cls.creator_role)
        cls.site = SharepointSite.objects.create(
            name="Committee Library",
            url="https://contoso.sharepoint.com/sites/committee",
            site_id="contoso,committee",
        )
        cls.drive = SharepointDrive.objects.create(
            site=cls.site, name="Documents", drive_id="drive-1"
        )
        SharepointSiteMember.objects.create(site=cls.site, user=cls.user)

    def setUp(self):
        self.client.force_login(self.user)

    def _make_bill(self, number="B 99—2026", **overrides):
        """A bill row direct from the ORM, owned by the creator."""
        data = {
            "workflow_type": self.bill_type,
            "current_state": self.bill_type.get_initial_state(),
            "title": "Filed bill",
            "owner": self.user,
            "bill_number": number,
        }
        data.update(overrides)
        return Bill.objects.create(**data)


@override_settings(BILL_DRIVE="drive-1", BILL_FOLDER="parent-1")
class FileBillFolderTests(_BillFixtureTestCase):
    """``file_bill_folder``: Graph filing + local mirror + the bill's link."""

    def test_a_created_folder_is_mirrored_onto_the_bill(self):
        bill = self._make_bill()
        with (
            patch.object(graph, "get_application_token", return_value=TOKEN),
            patch.object(
                bill_folders.graph,
                "create_child_folder",
                new=AsyncMock(return_value=(ITEM, True)),
            ),
        ):
            status = bill_folders.file_bill_folder(bill)

        self.assertEqual(status, bill_folders.CREATED)
        bill.refresh_from_db()
        self.assertEqual(bill.sharepoint_folder.folder_id, ITEM["id"])
        self.assertEqual(bill.sharepoint_folder.drive, self.drive)
        self.assertEqual(bill.sharepoint_folder.name, ITEM["name"])
        # The posted parent lives only on Graph so far: the mirror stands alone.
        self.assertIsNone(bill.sharepoint_folder.parent_folder)

    def test_an_already_filed_folder_is_linked_too(self):
        bill = self._make_bill()
        with (
            patch.object(graph, "get_application_token", return_value=TOKEN),
            patch.object(
                bill_folders.graph,
                "create_child_folder",
                new=AsyncMock(return_value=(ITEM, False)),
            ),
        ):
            status = bill_folders.file_bill_folder(bill)

        self.assertEqual(status, bill_folders.ALREADY_THERE)
        bill.refresh_from_db()
        self.assertEqual(bill.sharepoint_folder.folder_id, ITEM["id"])

    def test_an_unsynced_drive_is_resolved_from_the_graph(self):
        self.drive.delete()
        with (
            patch.object(graph, "get_application_token", return_value=TOKEN),
            patch.object(
                bill_folders.graph,
                "create_child_folder",
                new=AsyncMock(return_value=(ITEM, True)),
            ),
            patch.object(
                bill_folders.graph,
                "get_drive",
                new=AsyncMock(
                    return_value={
                        "name": "Documents",
                        "driveType": "documentLibrary",
                        "parentReference": {"siteId": "contoso,fresh"},
                    }
                ),
            ),
            patch.object(
                bill_folders.graph,
                "get_site",
                new=AsyncMock(
                    return_value={
                        "id": "contoso,fresh",
                        "name": "Fresh Site",
                        "webUrl": "https://contoso.sharepoint.com/sites/fresh",
                    }
                ),
            ),
        ):
            status = bill_folders.file_bill_folder(self._make_bill())

        self.assertEqual(status, bill_folders.CREATED)
        drive = SharepointDrive.objects.get(drive_id="drive-1")
        self.assertEqual(drive.site.site_id, "contoso,fresh")
        self.assertEqual(drive.site.name, "Fresh Site")
        bill = Bill.objects.get(bill_number="B 99—2026")
        self.assertEqual(bill.sharepoint_folder.drive, drive)

    def test_a_mirror_failure_keeps_the_filing_and_drops_the_link(self):
        self.drive.delete()  # No local row: forces the Graph lookup path.
        bill = self._make_bill()
        with (
            patch.object(graph, "get_application_token", return_value=TOKEN),
            patch.object(
                bill_folders.graph,
                "create_child_folder",
                new=AsyncMock(return_value=(ITEM, True)),
            ),
            patch.object(
                bill_folders.graph,
                "get_drive",
                new=AsyncMock(side_effect=Exception("net down")),
            ) as get_drive,
        ):
            status = bill_folders.file_bill_folder(bill)

        get_drive.assert_awaited_once()  # The failing lookup really ran.
        self.assertEqual(status, bill_folders.CREATED)  # The filing stands.
        bill.refresh_from_db()
        self.assertIsNone(bill.sharepoint_folder)  # Only the link fell away.

    def test_refiling_a_renumbered_bill_moves_the_link(self):
        old = attachments_service.resolve_folder(self.drive, "old-item", "B 1")
        bill = self._make_bill()
        bill.sharepoint_folder = old
        bill.save(update_fields=["sharepoint_folder"])
        bill.bill_number = "B 100—2026"
        bill.save(update_fields=["bill_number"])
        with (
            patch.object(graph, "get_application_token", return_value=TOKEN),
            patch.object(
                bill_folders.graph,
                "create_child_folder",
                new=AsyncMock(
                    return_value=({"id": "new-item", "name": "B 100—2026"}, True)
                ),
            ),
        ):
            status = bill_folders.file_bill_folder(bill)

        self.assertEqual(status, bill_folders.CREATED)
        bill.refresh_from_db()
        self.assertEqual(bill.sharepoint_folder.folder_id, "new-item")
        # The superseded folder stays on SharePoint: nothing is deleted here.
        self.assertTrue(SharepointFolder.objects.filter(folder_id="old-item").exists())


class BillViewFilingTests(_BillFixtureTestCase):
    """Saving a bill through the web form files its SharePoint folder."""

    def setUp(self):
        super().setUp()
        self.payload = {
            "workflow_type": self.bill_type.pk,
            "title": "Filed bill",
            "description": "",
            "owner": self.user.pk,
            "assigned_to": "",
            "deadline": "",
            "priority": "medium",
            "bill_number": "B 99—2026",
            "short_title": "Filed",
            "bill_type": Bill.SECTION_75,
            "house_of_origin": Bill.NA,
            "sponsor_name": "Minister of Justice",
            "introduced_date": "",
            "responsible_committee": "",
            "atc_reference": "",
            "order_paper_reference": "",
            "bill_document_url": "",
            "notes": "",
        }

    def _bill(self):
        return Bill.objects.get(bill_number="B 99—2026")

    @override_settings(BILL_DRIVE="drive-1", BILL_FOLDER="parent-1")
    def test_creating_a_bill_creates_and_links_its_folder(self):
        with (
            patch.object(graph, "get_application_token", return_value=TOKEN),
            patch.object(
                bill_folders.graph,
                "create_child_folder",
                new=AsyncMock(return_value=(ITEM, True)),
            ) as create,
        ):
            response = self.client.post(reverse("pwms:bill_create"), self.payload)

        self.assertRedirects(
            response,
            reverse("pwms:bill_detail", args=[self._bill().public_id]),
        )
        create.assert_awaited_once_with(TOKEN, "drive-1", "parent-1", "B 99—2026")
        self._bill().refresh_from_db()
        self.assertEqual(self._bill().sharepoint_folder.folder_id, ITEM["id"])

    @override_settings(BILL_DRIVE="drive-1", BILL_FOLDER="parent-1")
    def test_a_filing_failure_still_saves_the_bill(self):
        with (
            patch.object(graph, "get_application_token", return_value=TOKEN),
            patch.object(
                bill_folders.graph,
                "create_child_folder",
                new=AsyncMock(side_effect=Exception("Graph down")),
            ),
        ):
            response = self.client.post(
                reverse("pwms:bill_create"), self.payload, follow=True
            )

        self._bill()  # The save stands.
        warnings = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any("SharePoint folder" in m for m in warnings))

    @override_settings(BILL_DRIVE="", BILL_FOLDER="")
    def test_an_unconfigured_deployment_saves_without_a_warning(self):
        with (
            patch.object(graph, "get_application_token") as token,
            patch.object(bill_folders.graph, "create_child_folder") as create,
        ):
            response = self.client.post(
                reverse("pwms:bill_create"), self.payload, follow=True
            )

        self.assertRedirects(
            response,
            reverse("pwms:bill_detail", args=[self._bill().public_id]),
        )
        token.assert_not_called()
        create.assert_not_called()
        notices = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertFalse([m for m in notices if "SharePoint folder" in m])

    @override_settings(BILL_DRIVE="drive-1", BILL_FOLDER="parent-1")
    def test_the_chips_retry_refiles_a_details_page(self):
        bill = self._make_bill()
        with (
            patch.object(graph, "get_application_token", return_value=TOKEN),
            patch.object(
                bill_folders.graph,
                "create_child_folder",
                new=AsyncMock(return_value=(ITEM, True)),
            ),
        ):
            response = self.client.post(
                reverse("pwms:bill_file_folder", args=[bill.public_id]), follow=True
            )

        self.assertRedirects(
            response, reverse("pwms:bill_detail", args=[bill.public_id])
        )
        bill.refresh_from_db()
        self.assertEqual(bill.sharepoint_folder.folder_id, ITEM["id"])
        notices = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any("Filed its SharePoint folder" in m for m in notices))

    @override_settings(BILL_DRIVE="drive-1", BILL_FOLDER="parent-1")
    def test_refiling_is_an_edit_action(self):
        bill = self._make_bill()
        biller = get_user_model().objects.create_user(
            username="bystander", password="pw"
        )
        self.client.force_login(biller)
        with patch.object(bill_folders, "file_bill_folder") as file_it:
            response = self.client.post(
                reverse("pwms:bill_file_folder", args=[bill.public_id])
            )

        # The test client turns PermissionDenied into a 403 response.
        self.assertEqual(response.status_code, 403)
        file_it.assert_not_called()


class AttachmentUploadForBillsTests(_BillFixtureTestCase):
    """Uploads for a bill land in its own folder, not the picker's choice."""

    def setUp(self):
        super().setUp()
        self.bill = self._make_bill()

    def _post(self, **overrides):
        data = {
            "content_type": "pwms.bill",
            "object_id": str(self.bill.pk),
            "drive_id": self.drive.drive_id,
            "folder_id": "picked-7",
            "folder_name": "Elsewhere",
            "type": "document",
            "file": SimpleUploadedFile("minutes.pdf", b"%PDF-1.4"),
        }
        data.update(overrides)
        return self.client.post(reverse("pwms:attachment_upload"), data, follow=True)

    def test_an_upload_files_into_the_bills_folder(self):
        folder = attachments_service.resolve_folder(
            self.drive, ITEM["id"], ITEM["name"]
        )
        self.bill.sharepoint_folder = folder
        self.bill.save(update_fields=["sharepoint_folder"])
        with (
            patch.object(
                attachments_service.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments_service.graph,
                "upload_file",
                new=AsyncMock(return_value=_GRAPH_FILE),
            ) as upload,
        ):
            response = self._post()

        upload.assert_awaited_once_with(
            TOKEN, "drive-1", "child-item", "minutes.pdf", b"%PDF-1.4"
        )
        status = response.context["attachment_status"]
        self.assertEqual(status["level"], "success")
        self.assertIn('Filed into the bill folder "B 99—2026"', status["message"])
        attachment = Attachment.objects.get(item_id=_GRAPH_FILE["id"])
        self.assertEqual(attachment.sharepoint_folder, folder)

    def test_a_bill_without_a_filed_folder_uses_the_pickers_folder(self):
        with (
            patch.object(
                attachments_service.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments_service.graph,
                "upload_file",
                new=AsyncMock(return_value=_GRAPH_FILE),
            ) as upload,
        ):
            response = self._post()

        upload.assert_awaited_once_with(
            TOKEN, "drive-1", "picked-7", "minutes.pdf", b"%PDF-1.4"
        )
        self.assertNotIn("Filed into", response.context["attachment_status"]["message"])
