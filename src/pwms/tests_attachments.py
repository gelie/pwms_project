"""Tests for the SharePoint attachment picker: the service and the views.

Graph is never called for real — ``pwms.services.attachments.graph``'s async
helpers are replaced with ``AsyncMock``s, and ``get_application_token`` with a
stub — so the suite runs offline against the cached-token design.
"""

from importlib import import_module
from io import StringIO
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from .management.commands.populate_sites import Command
from .models import (
    Attachment,
    AttachmentVersion,
    Bill,
    DelegationReport,
    EventType,
    InternationalAgreement,
    InternationalResolution,
    SharepointDrive,
    SharepointSite,
    SharepointSiteMember,
    WorkflowEvent,
    WorkflowType,
)
from .services import attachments

User = get_user_model()

TOKEN = {"access_token": "test-token"}


def _graph_file(item_id="item-1", name="draft.pdf", size=2048):
    """A Graph drive item payload for a file."""
    return {
        "id": item_id,
        "name": name,
        "size": size,
        "file": {"mimeType": "application/pdf"},
        "webUrl": f"https://contoso.sharepoint.com/{name}",
        "@microsoft.graph.downloadUrl": f"https://contoso.sharepoint.com/dl/{name}",
    }


def _graph_version(
    version_id, modified="2026-09-01T09:00:00+00:00", size=100, author="Naledi K"
):
    """A Graph ``driveItemVersion`` payload."""
    return {
        "id": version_id,
        "size": size,
        "lastModifiedDateTime": modified,
        "lastModifiedBy": {"user": {"displayName": author}},
    }


def _graph_folder(item_id, name, child_count=0):
    """A Graph drive item payload for a folder."""
    return {
        "id": item_id,
        "name": name,
        "folder": {"childCount": child_count},
        "webUrl": f"https://contoso.sharepoint.com/{name}",
    }


class AttachmentTestCase(TestCase):
    """A report owned by ``owner``, a SharePoint site/drive, and a non-member."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="owner", password="pw")
        cls.stranger = User.objects.create_user(username="stranger", password="pw")
        report_type = WorkflowType.objects.get(name="Delegation Report")
        cls.report = DelegationReport.objects.create(
            workflow_type=report_type,
            current_state=report_type.get_initial_state(),
            title="Attachment test report",
            owner=cls.owner,
        )
        cls.site = SharepointSite.objects.create(
            name="Committee Library",
            url="https://contoso.sharepoint.com/sites/committee",
            site_id="contoso,committee",
        )
        cls.drive = SharepointDrive.objects.create(
            site=cls.site, name="Documents", drive_id="drive-1"
        )
        SharepointSiteMember.objects.create(site=cls.site, user=cls.owner)

    def _url(self, name, **params):
        """A picker URL for ``self.report``, with extra query parameters."""
        params.setdefault("content_type", "pwms.delegationreport")
        params.setdefault("object_id", str(self.report.pk))
        return f"{reverse(f'pwms:{name}')}?{urlencode(params)}"

    def _post_data(self, **overrides):
        """Form data naming ``self.report`` as the attachment target."""
        data = {
            "content_type": "pwms.delegationreport",
            "object_id": str(self.report.pk),
            "site_id": self.site.site_id,
            "drive_id": self.drive.drive_id,
            "folder_id": "root",
            "type": "document",
        }
        data.update(overrides)
        return data

    def _add_disabled_site(self):
        """A site the owner belongs to, but an administrator has disabled."""
        site = SharepointSite.objects.create(
            name="Retired Library",
            url="https://contoso.sharepoint.com/sites/retired",
            site_id="contoso,retired",
            enabled=False,
        )
        SharepointSiteMember.objects.create(site=site, user=self.owner)
        SharepointDrive.objects.create(
            site=site, name="Archive", drive_id="drive-retired"
        )
        return site

    def _attachment(self, **overrides):
        """An attachment on ``self.report`` in the fixture drive."""
        data = {
            "content_type": self.report._instance_ct(),
            "object_id": str(self.report.pk),
            "name": "draft.pdf",
            "drive_id": self.drive.drive_id,
            "item_id": "item-1",
            "type": "document",
            "uploaded_by": self.owner,
        }
        data.update(overrides)
        return Attachment.objects.create(**data)


class AttachmentServiceTests(AttachmentTestCase):
    def test_member_sites_lists_only_browsable_sites(self):
        SharepointSite.objects.create(
            name="Someone Else's Library",
            url="https://contoso.sharepoint.com/sites/other",
            site_id="contoso,other",
        )
        SharepointSite.objects.create(
            name="My OneDrive",
            url="https://contoso-my.sharepoint.com/personal/owner",
            site_id="contoso,owner",
            is_personal_site=True,
        )
        self.assertEqual(list(attachments.member_sites(self.owner)), [self.site])
        self.assertEqual(list(attachments.member_sites(self.stranger)), [])

    def test_superuser_may_browse_every_site(self):
        self.stranger.is_superuser = True
        self.stranger.save()
        self.assertIn(self.site, list(attachments.member_sites(self.stranger)))

    def test_require_site_access_rejects_a_non_member(self):
        with self.assertRaises(attachments.AttachmentError):
            attachments.require_site_access(self.stranger, self.site.site_id)

    def test_require_site_access_rejects_an_unknown_site(self):
        with self.assertRaises(attachments.AttachmentError):
            attachments.require_site_access(self.owner, "contoso,missing")

    def test_require_drive_access_resolves_its_site(self):
        drive = attachments.require_drive_access(self.owner, self.drive.drive_id)
        self.assertEqual(drive.site, self.site)

    def test_resolve_folder_mirrors_the_selected_folder(self):
        parent = attachments.resolve_folder(self.drive, "folder-parent", "Reports")
        child = attachments.resolve_folder(
            self.drive, "folder-child", "2026", parent_folder_id="folder-parent"
        )
        self.assertEqual(child.parent_folder, parent)
        self.assertEqual(child.get_full_path(), "/Reports/2026")

    def test_resolve_folder_ignores_the_drive_root(self):
        self.assertIsNone(attachments.resolve_folder(self.drive, "root"))
        self.assertIsNone(attachments.resolve_folder(self.drive, ""))

    def test_resolve_folder_holds_a_graph_sized_folder_id(self):
        # A folder id *is* a Graph driveItem id, and nothing this app controls
        # bounds its length: they run well past the 200 characters the column
        # used to allow, so no folder could be mirrored at all (migration 0038).
        folder_id = "01" + "A" * 398
        self.assertGreater(len(folder_id), 200)

        folder = attachments.resolve_folder(self.drive, folder_id, "Long id folder")

        folder.refresh_from_db()
        self.assertEqual(folder.folder_id, folder_id)
        # The id is the mirror's natural key, so a repeat selection finds the row.
        self.assertEqual(
            attachments.resolve_folder(self.drive, folder_id, "Long id folder").pk,
            folder.pk,
        )

    def test_open_url_resolves_a_pre_authenticated_link(self):
        attachment = self._attachment()
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph,
                "get_item",
                new=AsyncMock(return_value=_graph_file()),
            ) as get_item,
        ):
            url = attachments.open_url(attachment)

        get_item.assert_awaited_once_with(TOKEN, "drive-1", "item-1")
        # Graph's pre-authenticated link: it authenticates the fetch itself, so the
        # browser needs no SharePoint session.
        self.assertEqual(url, "https://contoso.sharepoint.com/dl/draft.pdf")

    def test_open_url_reports_a_document_graph_will_not_link(self):
        attachment = self._attachment()
        metadata = _graph_file()
        metadata.pop("@microsoft.graph.downloadUrl")
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph, "get_item", new=AsyncMock(return_value=metadata)
            ),
            self.assertRaises(attachments.AttachmentError),
        ):
            attachments.open_url(attachment)

    def test_folder_children_splits_folders_and_files(self):
        response = {
            "value": [
                _graph_folder("f2", "Reports", child_count=2),
                _graph_folder("f1", "Archive"),
                _graph_file("i1", "draft.pdf"),
            ]
        }
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph,
                "get_folder_items",
                new=AsyncMock(return_value=response),
            ) as get_items,
        ):
            folders, files = attachments.folder_children("drive-1", "root")

        get_items.assert_awaited_once_with(TOKEN, "drive-1", "root")
        self.assertEqual([folder["name"] for folder in folders], ["Archive", "Reports"])
        self.assertEqual([file["name"] for file in files], ["draft.pdf"])

    def test_graph_failures_become_attachment_errors(self):
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph,
                "get_folder_items",
                new=AsyncMock(side_effect=Exception("boom")),
            ),
            self.assertRaises(attachments.AttachmentError),
        ):
            attachments.folder_children("drive-1", "root")

    def test_link_document_stores_the_sharepoint_item(self):
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph,
                "get_item",
                new=AsyncMock(return_value=_graph_file()),
            ) as get_item,
        ):
            attachment = attachments.link_document(
                obj=self.report,
                user=self.owner,
                drive=self.drive,
                item_id="item-1",
                folder=attachments.resolve_folder(self.drive, "folder-1", "Letters"),
            )

        get_item.assert_awaited_once_with(TOKEN, "drive-1", "item-1")
        self.assertEqual(attachment.content_object, self.report)
        self.assertEqual(attachment.item_id, "item-1")
        self.assertEqual(attachment.sharepoint_site, self.site)
        self.assertEqual(attachment.sharepoint_drive, self.drive)
        self.assertEqual(attachment.sharepoint_folder_path, "/Letters")
        self.assertEqual(attachment.type, "document")
        self.assertEqual(attachment.uploaded_by, self.owner)
        self.assertEqual(self.report.attachments.count(), 1)

    def test_link_document_is_idempotent_per_record_item(self):
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph, "get_item", new=AsyncMock(return_value=_graph_file())
            ),
        ):
            attachments.link_document(
                obj=self.report, user=self.owner, drive=self.drive, item_id="item-1"
            )
            with self.assertRaises(attachments.AttachmentError):
                attachments.link_document(
                    obj=self.report, user=self.owner, drive=self.drive, item_id="item-1"
                )

    def test_link_document_defaults_an_unknown_type(self):
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph, "get_item", new=AsyncMock(return_value=_graph_file())
            ),
        ):
            attachment = attachments.link_document(
                obj=self.report,
                user=self.owner,
                drive=self.drive,
                item_id="item-1",
                attachment_type="nonsense",
            )
        self.assertEqual(attachment.type, "document")

    def test_upload_document_uploads_into_the_selected_folder(self):
        folder = attachments.resolve_folder(self.drive, "folder-9", "Minutes")
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph,
                "upload_file",
                new=AsyncMock(return_value=_graph_file("item-9", "minutes.pdf")),
            ) as upload,
        ):
            attachment, created = attachments.upload_document(
                obj=self.report,
                user=self.owner,
                drive=self.drive,
                filename="minutes.pdf",
                content=b"%PDF-1.4",
                folder=folder,
            )

        upload.assert_awaited_once_with(
            TOKEN, "drive-1", "folder-9", "minutes.pdf", b"%PDF-1.4"
        )
        self.assertEqual(attachment.item_id, "item-9")
        self.assertEqual(attachment.sharepoint_folder, folder)
        self.assertTrue(created)

    def test_upload_document_into_the_root_uses_the_root_folder_id(self):
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph,
                "upload_file",
                new=AsyncMock(return_value=_graph_file("item-9", "minutes.pdf")),
            ) as upload,
        ):
            attachments.upload_document(
                obj=self.report,
                user=self.owner,
                drive=self.drive,
                filename="minutes.pdf",
                content=b"x",
            )
        self.assertEqual(upload.await_args.args[2], "root")

    def test_member_sites_excludes_a_disabled_site(self):
        self._add_disabled_site()
        names = [site.name for site in attachments.member_sites(self.owner)]
        self.assertNotIn("Retired Library", names)

    def test_superuser_does_not_see_a_disabled_site(self):
        self._add_disabled_site()
        self.stranger.is_superuser = True
        self.stranger.save()
        names = [site.name for site in attachments.member_sites(self.stranger)]
        self.assertNotIn("Retired Library", names)

    def test_require_site_access_rejects_a_disabled_site(self):
        site = self._add_disabled_site()
        with self.assertRaises(attachments.AttachmentError):
            attachments.require_site_access(self.owner, site.site_id)

    def test_require_site_access_rejects_a_disabled_site_for_superusers(self):
        site = self._add_disabled_site()
        self.stranger.is_superuser = True
        self.stranger.save()
        with self.assertRaises(attachments.AttachmentError):
            attachments.require_site_access(self.stranger, site.site_id)

    def test_require_drive_access_rejects_a_drive_on_a_disabled_site(self):
        self._add_disabled_site()
        with self.assertRaises(attachments.AttachmentError):
            attachments.require_drive_access(self.owner, "drive-retired")

    def test_sync_versions_mirrors_sharepoint_history(self):
        attachment = self._attachment()
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph,
                "get_item_versions",
                new=AsyncMock(
                    return_value={
                        "value": [
                            _graph_version(
                                "2.0",
                                "2026-09-02T09:00:00+00:00",
                                size=300,
                                author="Thabo M",
                            ),
                            _graph_version("1.0", size=100, author="Naledi K"),
                        ]
                    }
                ),
            ),
        ):
            synced = list(attachments.sync_versions(attachment))

        self.assertEqual([version.version_id for version in synced], ["2.0", "1.0"])
        self.assertEqual(synced[0].modified_by, "Thabo M")
        self.assertEqual(synced[0].size, 300)
        self.assertTrue(synced[0].is_current)
        self.assertFalse(synced[1].is_current)
        self.assertEqual(
            AttachmentVersion.objects.filter(attachment=attachment).count(), 2
        )

    def test_sync_versions_refreshes_without_duplicating(self):
        attachment = self._attachment()
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph,
                "get_item_versions",
                new=AsyncMock(
                    return_value={"value": [_graph_version("1.0", size=100)]}
                ),
            ),
        ):
            attachments.sync_versions(attachment)
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph,
                "get_item_versions",
                new=AsyncMock(
                    return_value={
                        "value": [
                            _graph_version("2.0", "2026-09-02T09:00:00+00:00"),
                            _graph_version("1.0", size=150),
                        ]
                    }
                ),
            ),
        ):
            synced = list(attachments.sync_versions(attachment))

        self.assertEqual(len(synced), 2)
        first = AttachmentVersion.objects.get(attachment=attachment, version_id="1.0")
        self.assertEqual(first.size, 150)
        self.assertFalse(first.is_current)

    def test_sync_versions_reports_graph_failures(self):
        attachment = self._attachment()
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph,
                "get_item_versions",
                new=AsyncMock(side_effect=Exception("boom")),
            ),
            self.assertRaises(attachments.AttachmentError),
        ):
            attachments.sync_versions(attachment)

    def test_version_download_url_resolves_the_sharepoint_link(self):
        attachment = self._attachment()
        version = AttachmentVersion.objects.create(
            attachment=attachment, version_id="1.0"
        )
        signed = "https://contoso.sharepoint.com/signed/1.0"
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph,
                "get_version_download_url",
                new=AsyncMock(return_value=signed),
            ) as resolver,
        ):
            url = attachments.version_download_url(attachment, version)

        resolver.assert_awaited_once_with(TOKEN, "drive-1", "item-1", "1.0")
        self.assertEqual(url, signed)

    def _link(self, item_id="item-1"):
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph,
                "get_item",
                new=AsyncMock(return_value=_graph_file(item_id)),
            ),
        ):
            return attachments.link_document(
                obj=self.report,
                user=self.owner,
                drive=self.drive,
                item_id=item_id,
            )

    def test_link_records_an_attach_event(self):
        self._link()

        events = attachments.attachment_activity(self.report)
        self.assertEqual(
            [event.event_type.slug for event in events], ["document-attached"]
        )
        self.assertEqual(events[0].payload["name"], "draft.pdf")
        self.assertEqual(events[0].payload["item_id"], "item-1")
        self.assertEqual(events[0].actor, self.owner)

    def test_detach_records_the_document_snapshot(self):
        attachment = self._link()

        attachments.detach_document(attachment, actor=self.owner)

        events = attachments.attachment_activity(self.report)
        self.assertEqual(
            [event.event_type.slug for event in events],
            ["document-detached", "document-attached"],
        )
        detached = events[0]
        # The payload survives the row it describes.
        self.assertEqual(detached.payload["name"], "draft.pdf")
        self.assertEqual(detached.payload["item_id"], "item-1")
        self.assertEqual(detached.actor, self.owner)

    def test_reuploading_a_file_records_a_new_version(self):
        original = self._link()
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph,
                "upload_file",
                new=AsyncMock(
                    return_value=_graph_file("item-1", "draft.pdf", size=4096)
                ),
            ),
        ):
            refreshed, created = attachments.upload_document(
                obj=self.report,
                user=self.owner,
                drive=self.drive,
                filename="draft.pdf",
                content=b"revised",
            )

        self.assertFalse(created)
        self.assertEqual(refreshed.pk, original.pk)
        self.assertEqual(refreshed.size, 4096)
        self.assertEqual(self.report.attachments.count(), 1)
        slugs = [
            event.event_type.slug
            for event in attachments.attachment_activity(self.report)
        ]
        self.assertEqual(slugs, ["document-version-added", "document-attached"])


class AttachmentViewTests(AttachmentTestCase):
    def _open_url(self, attachment):
        """The endpoint a document's name links to."""
        return reverse("pwms:attachment_open", args=[attachment.public_id])

    def test_browser_lists_the_users_sites(self):
        self.client.force_login(self.owner)
        response = self.client.get(self._url("attachment_browser"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Committee Library")
        self.assertContains(response, "attachment-folder-contents")

    def test_browser_requires_edit_permission(self):
        self.client.force_login(self.stranger)
        response = self.client.get(self._url("attachment_browser"))
        self.assertEqual(response.status_code, 403)

    def test_browser_rejects_an_unknown_target(self):
        self.client.force_login(self.owner)
        response = self.client.get(self._url("attachment_browser", object_id="999999"))
        self.assertEqual(response.status_code, 404)

    def test_tree_lists_a_sites_drives(self):
        self.client.force_login(self.owner)
        response = self.client.get(
            self._url("attachment_tree_children", site_id=self.site.site_id)
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Documents")

    def test_tree_lists_a_drives_folders(self):
        self.client.force_login(self.owner)
        response = {
            "value": [_graph_folder("f1", "Reports"), _graph_file("i1", "draft.pdf")]
        }
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph,
                "get_folder_items",
                new=AsyncMock(return_value=response),
            ),
        ):
            page = self.client.get(
                self._url("attachment_tree_children", drive_id="drive-1")
            )
        self.assertContains(page, "Reports")
        self.assertNotContains(page, "draft.pdf")

    def test_tree_reports_a_site_the_user_cannot_browse(self):
        # A user who may edit the record but is not a site member: the tree
        # endpoint authorises the record, then refuses the site itself.
        report_type = WorkflowType.objects.get(name="Delegation Report")
        owned_by_stranger = DelegationReport.objects.create(
            workflow_type=report_type,
            current_state=report_type.get_initial_state(),
            title="Stranger's report",
            owner=self.stranger,
        )
        self.client.force_login(self.stranger)
        response = self.client.get(
            f"{reverse('pwms:attachment_tree_children')}?"
            + urlencode(
                {
                    "content_type": "pwms.delegationreport",
                    "object_id": str(owned_by_stranger.pk),
                    "site_id": self.site.site_id,
                }
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not a member")

    def test_folder_lists_files_with_an_attach_action(self):
        self.client.force_login(self.owner)
        response = {"value": [_graph_file("i1", "draft.pdf")]}
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph,
                "get_folder_items",
                new=AsyncMock(return_value=response),
            ),
        ):
            page = self.client.get(
                self._url(
                    "attachment_folder",
                    drive_id="drive-1",
                    folder_id="root",
                    folder_name="Root",
                )
            )
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "draft.pdf")
        self.assertContains(page, "Attach")
        self.assertContains(page, "Upload &amp; attach")

    def test_link_attaches_the_selected_document(self):
        self.client.force_login(self.owner)
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph, "get_item", new=AsyncMock(return_value=_graph_file())
            ),
        ):
            response = self.client.post(
                reverse("pwms:attachment_link"),
                self._post_data(item_id="item-1", folder_name="Root"),
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "draft.pdf")
        self.assertContains(response, "attached")
        self.assertEqual(self.report.attachments.count(), 1)

    def test_link_attaches_a_document_inside_a_graph_sized_folder(self):
        """A real Graph folder id is longer than 200 characters (migration 0038)."""
        folder_id = "01" + "B" * 398
        self.client.force_login(self.owner)
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph, "get_item", new=AsyncMock(return_value=_graph_file())
            ),
        ):
            response = self.client.post(
                reverse("pwms:attachment_link"),
                self._post_data(
                    item_id="item-1", folder_id=folder_id, folder_name="Deep folder"
                ),
            )
        self.assertEqual(response.status_code, 200)
        attachment = self.report.attachments.get()
        self.assertEqual(attachment.sharepoint_folder.folder_id, folder_id)

    def test_link_records_a_document_event_for_a_long_sharepoint_url(self):
        """The reported 500: a Graph ``webUrl`` longer than the event column.

        ``Attachment.sharepoint_web_url`` allows 2048 characters, and the
        ``document-attached`` event copies that URL into ``WorkflowEvent``. A
        narrower column there let the attachment through and *then* raised, so the
        record showed the document with no activity for it (migration 0038).
        """
        long_url = "https://contoso.sharepoint.com/sites/committee/" + "C" * 240
        metadata = _graph_file()
        metadata["webUrl"] = long_url
        self.assertGreater(len(long_url), 200)

        self.client.force_login(self.owner)
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph, "get_item", new=AsyncMock(return_value=metadata)
            ),
        ):
            response = self.client.post(
                reverse("pwms:attachment_link"), self._post_data(item_id="item-1")
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.report.attachments.get().sharepoint_web_url,
            long_url,
        )
        event = self.report.events().get(event_type__slug="document-attached")
        self.assertEqual(event.document_url, long_url)
        # The row and its activity entry both open through the app, which resolves
        # the document with the application token rather than sending the reader to
        # SharePoint to sign in.
        attachment = self.report.attachments.get()
        self.assertContains(response, self._open_url(attachment), count=2)

    def test_a_document_name_opens_through_the_app(self):
        attachment = self._attachment()
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("pwms:delegation_report_detail", args=[self.report.public_id])
        )

        self.assertContains(response, self._open_url(attachment))

    def test_open_redirects_to_a_fresh_pre_authenticated_url(self):
        attachment = self._attachment()
        self.client.force_login(self.owner)
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph,
                "get_item",
                new=AsyncMock(return_value=_graph_file()),
            ) as get_item,
        ):
            response = self.client.get(self._open_url(attachment))

        get_item.assert_awaited_once_with(TOKEN, "drive-1", "item-1")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"], "https://contoso.sharepoint.com/dl/draft.pdf"
        )

    def test_open_requires_view_permission(self):
        attachment = self._attachment()
        self.client.force_login(self.stranger)
        response = self.client.get(self._open_url(attachment))
        self.assertEqual(response.status_code, 403)

    def test_open_reports_a_sharepoint_failure(self):
        attachment = self._attachment()
        self.client.force_login(self.owner)
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph,
                "get_item",
                new=AsyncMock(side_effect=Exception("boom")),
            ),
        ):
            response = self.client.get(self._open_url(attachment), follow=True)

        self.assertEqual(response.status_code, 200)
        # The reader lands back on the record with the reason shown as a message.
        self.assertContains(response, "SharePoint request failed")

    def test_link_reports_a_duplicate_attachment(self):
        self.client.force_login(self.owner)
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph, "get_item", new=AsyncMock(return_value=_graph_file())
            ),
        ):
            self.client.post(
                reverse("pwms:attachment_link"), self._post_data(item_id="item-1")
            )
            response = self.client.post(
                reverse("pwms:attachment_link"), self._post_data(item_id="item-1")
            )
        self.assertContains(response, "already attached")
        self.assertEqual(self.report.attachments.count(), 1)

    def test_link_requires_edit_permission(self):
        self.client.force_login(self.stranger)
        response = self.client.post(
            reverse("pwms:attachment_link"), self._post_data(item_id="item-1")
        )
        self.assertEqual(response.status_code, 403)

    def test_link_rejects_get(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("pwms:attachment_link"))
        self.assertEqual(response.status_code, 405)

    def test_upload_attaches_the_uploaded_file(self):
        self.client.force_login(self.owner)
        upload = SimpleUploadedFile(
            "minutes.pdf", b"%PDF-1.4", content_type="application/pdf"
        )
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph,
                "upload_file",
                new=AsyncMock(return_value=_graph_file("item-9", "minutes.pdf")),
            ) as upload_call,
        ):
            response = self.client.post(
                reverse("pwms:attachment_upload"),
                self._post_data(
                    file=upload, folder_id="folder-9", folder_name="Minutes"
                ),
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "minutes.pdf")
        upload_call.assert_awaited_once()
        attachment = self.report.attachments.get()
        self.assertEqual(attachment.item_id, "item-9")
        self.assertEqual(attachment.sharepoint_folder.folder_id, "folder-9")

    def test_upload_without_a_file_reports_an_error(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("pwms:attachment_upload"),
            {k: v for k, v in self._post_data().items() if k != "file"},
        )
        self.assertContains(response, "No file was selected")
        self.assertEqual(self.report.attachments.count(), 0)

    def test_upload_requires_edit_permission(self):
        self.client.force_login(self.stranger)
        upload = SimpleUploadedFile("minutes.pdf", b"x")
        response = self.client.post(
            reverse("pwms:attachment_upload"), self._post_data(file=upload)
        )
        self.assertEqual(response.status_code, 403)

    def test_delete_detaches_the_document(self):
        attachment = Attachment.objects.create(
            content_type=self.report._instance_ct(),
            object_id=str(self.report.pk),
            name="draft.pdf",
            drive_id="drive-1",
            item_id="item-1",
            type="document",
            uploaded_by=self.owner,
        )
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("pwms:attachment_delete", args=[attachment.public_id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Attachment.objects.filter(pk=attachment.pk).exists())
        self.assertContains(response, "removed")

    def test_a_detach_refreshes_the_counters_out_of_band(self):
        self._attachment(name="keep.pdf", item_id="item-1")
        doomed = self._attachment(name="drop.pdf", item_id="item-2")
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("pwms:attachment_delete", args=[doomed.public_id]),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        # Neither counter is inside #attachment-list, so both arrive out-of-band.
        self.assertContains(
            response,
            '<span class="workflow-tab-count" id="tab-attachments-count" hx-swap-oob="true">1</span>',
        )
        self.assertContains(
            response,
            '<span class="badge badge-muted" id="attachment-count" hx-swap-oob="true">1</span>',
        )
        # The record-level counters ride along, so the whole tab bar stays fresh.
        self.assertContains(response, 'id="tab-progress-count" hx-swap-oob="true"')
        self.assertContains(response, 'id="tab-timeline-count" hx-swap-oob="true"')

    def test_delete_requires_edit_permission(self):
        attachment = Attachment.objects.create(
            content_type=self.report._instance_ct(),
            object_id=str(self.report.pk),
            name="draft.pdf",
            drive_id="drive-1",
            item_id="item-1",
            type="document",
        )
        self.client.force_login(self.stranger)
        response = self.client.post(
            reverse("pwms:attachment_delete", args=[attachment.public_id])
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Attachment.objects.filter(pk=attachment.pk).exists())

    def test_detail_page_renders_the_attachment_section(self):
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("pwms:delegation_report_detail", args=[self.report.public_id])
        )
        self.assertContains(response, "Attachments")
        self.assertContains(response, "Add attachment")
        self.assertContains(response, "attachment-browser")

    def test_browser_hides_a_disabled_site(self):
        self._add_disabled_site()
        self.client.force_login(self.owner)
        response = self.client.get(self._url("attachment_browser"))
        self.assertContains(response, "Committee Library")
        self.assertNotContains(response, "Retired Library")

    def test_tree_reports_a_disabled_site(self):
        site = self._add_disabled_site()
        self.client.force_login(self.owner)
        response = self.client.get(
            self._url("attachment_tree_children", site_id=site.site_id)
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "disabled")

    def test_link_rejects_a_drive_on_a_disabled_site(self):
        self._add_disabled_site()
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("pwms:attachment_link"),
            self._post_data(item_id="item-1", drive_id="drive-retired"),
        )
        self.assertContains(response, "disabled")
        self.assertEqual(self.report.attachments.count(), 0)

    def test_versions_panel_lists_sharepoint_versions(self):
        attachment = self._attachment()
        self.client.force_login(self.owner)
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph,
                "get_item_versions",
                new=AsyncMock(return_value={"value": [_graph_version("1.0")]}),
            ),
        ):
            response = self.client.get(
                reverse("pwms:attachment_versions", args=[attachment.public_id])
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "v1.0")
        self.assertContains(response, "Current")
        self.assertEqual(
            AttachmentVersion.objects.filter(attachment=attachment).count(), 1
        )

    def test_versions_panel_requires_view_permission(self):
        attachment = self._attachment()
        self.client.force_login(self.stranger)
        response = self.client.get(
            reverse("pwms:attachment_versions", args=[attachment.public_id])
        )
        self.assertEqual(response.status_code, 403)

    def test_versions_panel_reports_a_graph_failure(self):
        attachment = self._attachment()
        self.client.force_login(self.owner)
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph,
                "get_item_versions",
                new=AsyncMock(side_effect=Exception("boom")),
            ),
        ):
            response = self.client.get(
                reverse("pwms:attachment_versions", args=[attachment.public_id])
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SharePoint request failed")

    def test_version_download_redirects_to_sharepoint(self):
        attachment = self._attachment()
        version = AttachmentVersion.objects.create(
            attachment=attachment, version_id="1.0"
        )
        signed = "https://contoso.sharepoint.com/signed/1.0"
        self.client.force_login(self.owner)
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph,
                "get_version_download_url",
                new=AsyncMock(return_value=signed),
            ),
        ):
            response = self.client.get(
                reverse("pwms:attachment_version_download", args=[version.public_id])
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], signed)

    def test_version_download_requires_view_permission(self):
        attachment = self._attachment()
        version = AttachmentVersion.objects.create(
            attachment=attachment, version_id="1.0"
        )
        self.client.force_login(self.stranger)
        response = self.client.get(
            reverse("pwms:attachment_version_download", args=[version.public_id])
        )
        self.assertEqual(response.status_code, 403)

    def test_version_download_reports_a_missing_link(self):
        attachment = self._attachment()
        version = AttachmentVersion.objects.create(
            attachment=attachment, version_id="1.0"
        )
        self.client.force_login(self.owner)
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph,
                "get_version_download_url",
                new=AsyncMock(return_value=""),
            ),
        ):
            response = self.client.get(
                reverse("pwms:attachment_version_download", args=[version.public_id])
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.report.get_absolute_url())

    def test_activity_trail_shows_attach_and_detach(self):
        self.client.force_login(self.owner)
        with (
            patch.object(
                attachments.graph, "get_application_token", return_value=TOKEN
            ),
            patch.object(
                attachments.graph,
                "get_item",
                new=AsyncMock(return_value=_graph_file()),
            ),
        ):
            attach_response = self.client.post(
                reverse("pwms:attachment_link"),
                self._post_data(item_id="item-1", folder_name="Root"),
            )
        self.assertContains(attach_response, "Document attached")

        attachment = self.report.attachments.get()
        detach_response = self.client.post(
            reverse("pwms:attachment_delete", args=[attachment.public_id])
        )
        self.assertContains(detach_response, "Document detached")
        self.assertContains(detach_response, "draft.pdf")


class AttachmentDetailPageTests(AttachmentTestCase):
    def test_every_workflow_detail_page_renders_the_attachment_section(self):
        resolution_type = WorkflowType.objects.get(name="International Resolution")
        resolution = InternationalResolution.objects.create(
            workflow_type=resolution_type,
            current_state=resolution_type.get_initial_state(),
            resolution_number="IR-ATT-1",
            title="Resolution with attachments",
            owner=self.owner,
        )
        agreement_type = WorkflowType.objects.get(name="International Agreement")
        agreement = InternationalAgreement.objects.create(
            workflow_type=agreement_type,
            current_state=agreement_type.get_initial_state(),
            title="Agreement with attachments",
            owner=self.owner,
        )
        bill_type = WorkflowType.objects.get(name="Bill")
        bill = Bill.objects.create(
            workflow_type=bill_type,
            current_state=bill_type.get_initial_state(),
            bill_number="B 99—2026",
            title="Bill with attachments",
            owner=self.owner,
        )
        pages = {
            "delegation_report_detail": self.report,
            "international_resolution_detail": resolution,
            "international_agreement_detail": agreement,
            "bill_detail": bill,
        }

        self.client.force_login(self.owner)
        for view_name, instance in pages.items():
            with self.subTest(view=view_name):
                response = self.client.get(
                    reverse(f"pwms:{view_name}", args=[instance.public_id])
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Attachments")
                self.assertContains(response, "Add attachment")


class AttachmentTypeChoiceTests(TestCase):
    """The attachment type list is the picker's dropdown, so it is order-sensitive."""

    def test_choices_are_sorted_alphabetically_by_label(self):
        labels = [label for _, label in Attachment.ATTACHMENT_TYPE_CHOICES]
        self.assertEqual(labels, sorted(labels))

    def test_choices_cover_the_workflow_document_types(self):
        values = {value for value, _ in Attachment.ATTACHMENT_TYPE_CHOICES}
        self.assertLessEqual(
            {
                "agreement",
                "bill",
                "document",
                "motion",
                "petition",
                "question",
                "report",
                "resolution",
                "response",
            },
            values,
        )

    def test_choice_values_are_unique(self):
        values = [value for value, _ in Attachment.ATTACHMENT_TYPE_CHOICES]
        self.assertEqual(len(values), len(set(values)))


class SharepointSiteSyncTests(TestCase):
    """The tenant sync must leave the locally-curated ``enabled`` flag alone.

    ``enabled`` gates the attachment picker, so an administrator's decision to
    hide a library has to survive re-running ``populate_sites``.
    """

    def _process_sites(self, sites):
        """Run the command's site pass against a stub Graph response."""
        command = Command()
        command.stdout = StringIO()
        command.stderr = StringIO()
        with patch.object(command, "_process_drives"):
            command._process_sites({"value": sites}, {"access_token": "test-token"})

    def _graph_site(self, site_id, name, modified=None):
        payload = {
            "id": site_id,
            "displayName": name,
            "webUrl": f"https://contoso.sharepoint.com/sites/{site_id}",
        }
        if modified:
            payload["lastModifiedDateTime"] = modified
        return payload

    def test_resync_keeps_a_disabled_site_disabled(self):
        site = SharepointSite.objects.create(
            name="Old name",
            url="https://contoso.sharepoint.com/sites/committee",
            site_id="contoso,committee",
            enabled=False,
        )
        self._process_sites(
            [
                self._graph_site(
                    "contoso,committee",
                    "Renamed library",
                    "2026-09-01T10:00:00+00:00",
                )
            ]
        )
        site.refresh_from_db()
        # The mirrored fields were refreshed, but not the curated flag.
        self.assertEqual(site.name, "Renamed library")
        self.assertFalse(site.enabled)

    def test_new_sites_are_enabled_by_default(self):
        self._process_sites([self._graph_site("contoso,new", "New library")])
        self.assertTrue(SharepointSite.objects.get(site_id="contoso,new").enabled)


class SharepointSiteAdminTests(TestCase):
    """``enabled`` is curated straight from the admin changelist."""

    @classmethod
    def setUpTestData(cls):
        cls.admin_user = User.objects.create_superuser(
            username="site-admin", password="pw"
        )
        cls.site = SharepointSite.objects.create(
            name="Committee Library",
            url="https://contoso.sharepoint.com/sites/committee",
            site_id="contoso,committee",
        )

    def setUp(self):
        self.client.force_login(self.admin_user)

    def _changelist(self):
        return reverse("admin:pwms_sharepointsite_changelist")

    def test_changelist_lists_sites_without_their_site_id(self):
        response = self.client.get(self._changelist())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Committee Library")
        self.assertNotContains(response, self.site.site_id)

    def test_changelist_offers_enabled_as_an_editable_column(self):
        response = self.client.get(self._changelist())
        self.assertContains(response, "form-0-enabled")

    def test_changelist_can_disable_a_site(self):
        response = self.client.post(
            self._changelist(),
            {
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "1",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-id": str(self.site.pk),
                # ``form-0-enabled`` is omitted, i.e. the box was left unchecked.
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.site.refresh_from_db()
        self.assertFalse(self.site.enabled)


class AttachmentEventTypeTests(TestCase):
    """The document event types are seeded reference data (migration 0031)."""

    def test_attachment_event_types_are_seeded(self):
        expected = {
            "document-attached",
            "document-detached",
            "document-version-added",
        }
        seeded = set(
            EventType.objects.filter(slug__in=expected).values_list("slug", flat=True)
        )
        self.assertEqual(seeded, expected)


class DocumentEventBackfillTests(TestCase):
    """Migration 0039 restores document events lost to the old 200-character column."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="backfill-owner", password="pw")
        report_type = WorkflowType.objects.get(name="Delegation Report")
        cls.report = DelegationReport.objects.create(
            workflow_type=report_type,
            current_state=report_type.get_initial_state(),
            title="Backfill test report",
            owner=cls.owner,
        )
        cls.event_type = EventType.objects.get(slug="document-attached")

    def _migration(self):
        return import_module("pwms.migrations.0039_backfill_document_attached_events")

    def _attachment(self, name="filed.pdf", **overrides):
        """An attachment on the report with no event recorded for it."""
        data = {
            "content_type": self.report._instance_ct(),
            "object_id": str(self.report.pk),
            "name": name,
            "drive_id": "drive-1",
            "item_id": f"item-{name}",
            "type": "document",
            "uploaded_by": self.owner,
            "sharepoint_web_url": (
                "https://contoso.sharepoint.com/sites/committee/filed.pdf"
            ),
            "download_url": "https://contoso.sharepoint.com/dl/filed.pdf",
        }
        data.update(overrides)
        return Attachment.objects.create(**data)

    def _events(self):
        return WorkflowEvent.objects.filter(
            content_type=self.report._instance_ct(),
            object_id=str(self.report.pk),
            event_type=self.event_type,
        )

    def test_the_missing_event_is_reconstructed_from_the_attachment(self):
        attachment = self._attachment()
        attachment.refresh_from_db()

        self._migration().backfill_document_events(apps, None)

        event = self._events().get()
        # Written long after the fact, so the system wrote it and nobody is named.
        self.assertEqual(event.origin, "system")
        self.assertIsNone(event.actor)
        # The link the activity panel renders is the web URL, not the download URL.
        self.assertEqual(event.document_url, attachment.sharepoint_web_url)
        self.assertEqual(event.occurred_at, attachment.created_at)
        self.assertEqual(event.payload["attachment_id"], str(attachment.public_id))
        self.assertTrue(event.payload["backfilled"])

    def test_an_attachment_whose_event_was_recorded_is_left_alone(self):
        attachment = self._attachment()
        self.report.record_event(
            self.event_type,
            actor=self.owner,
            payload={"attachment_id": str(attachment.public_id)},
            document_url=attachment.sharepoint_web_url,
        )

        self._migration().backfill_document_events(apps, None)

        self.assertEqual(self._events().count(), 1)

    def test_an_event_without_a_payload_still_counts_as_the_attachments(self):
        # `seed_demo_data` records the event with no payload, so it cannot name its
        # attachment; the target's counts match, so nothing is invented for it.
        self._attachment()
        self.report.record_event(self.event_type, actor=self.owner)

        self._migration().backfill_document_events(apps, None)

        self.assertEqual(self._events().count(), 1)

    def test_the_reverse_removes_only_the_reconstructed_events(self):
        recorded = self._attachment(name="recorded.pdf")
        self.report.record_event(
            self.event_type,
            actor=self.owner,
            payload={"attachment_id": str(recorded.public_id)},
        )
        self._attachment(name="lost.pdf")

        migration = self._migration()
        migration.backfill_document_events(apps, None)
        self.assertEqual(self._events().count(), 2)

        migration.unbackfill_document_events(apps, None)

        self.assertEqual(self._events().count(), 1)
        self.assertFalse(self._events().get().payload.get("backfilled"))


class AttachmentActivityRenderingTests(TestCase):
    """The document-activity panel copes with whatever a payload holds.

    ``seed_demo_data`` records document events with no payload at all (``payload
    or {}``), so the panel must not read a key straight out of that dict: with
    ``DEBUG`` on, Django does not swallow a failed lookup inside ``{% if %}`` /
    ``{% with %}``, and the whole detail page answers 500 instead of rendering.
    """

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="activity-owner", password="pw")
        report_type = WorkflowType.objects.get(name="Delegation Report")
        cls.report = DelegationReport.objects.create(
            workflow_type=report_type,
            current_state=report_type.get_initial_state(),
            title="Activity render report",
            owner=cls.owner,
        )
        cls.event_type = EventType.objects.get(slug="document-attached")

    def setUp(self):
        self.client.force_login(self.owner)

    def _detail_url(self):
        return reverse(
            "pwms:delegation_report_detail",
            kwargs={"public_id": self.report.public_id},
        )

    def test_a_document_event_without_a_payload_still_renders(self):
        self.report.record_event(self.event_type, actor=self.owner)

        with override_settings(DEBUG=True):
            response = self.client.get(self._detail_url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Document activity")

    def test_a_document_event_without_a_payload_offers_no_open_link(self):
        """It cannot name the document, so the trail offers no link at all."""
        self.report.record_event(self.event_type, actor=self.owner)

        with override_settings(DEBUG=True):
            response = self.client.get(self._detail_url())

        self.assertNotContains(response, "open</a>")
