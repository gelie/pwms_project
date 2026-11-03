import sys
import unittest

import django

django.setup()

from django.conf import settings

# The Django test runner adds "testserver" to ALLOWED_HOSTS; a standalone
# unittest run must do it itself.
if "testserver" not in settings.ALLOWED_HOSTS:
    settings.ALLOWED_HOSTS = [*settings.ALLOWED_HOSTS, "testserver"]

loader = unittest.TestLoader()
names = [
    "pwms.tests_bill_folders.FolderNameTests",
    "pwms.tests_bill_folders.EnsureBillFolderTests",
    "pwms.tests_bill_folders.EnsureBillFolderConfigTests",
    "pwms.tests_bill_folders.BillFolderPreviewTests",
]
suite = loader.loadTestsFromNames(names)
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
