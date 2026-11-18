"""Check that the target image honors the approved Paperless policies.

To run this isolated check, set HOMELAB_CONTAINER_TESTS=1 and run
`.venv/bin/python -m unittest discover -s tests -p test_paperless_v3_settings.py`.
Set HOMELAB_TEST_REF to compare an earlier compose revision.
"""

import json
import os
from pathlib import Path
import subprocess
import unittest

import yaml


@unittest.skipUnless(os.environ.get("HOMELAB_CONTAINER_TESTS") == "1", "opt-in container check")
class PaperlessV3SettingsTest(unittest.TestCase):
    def test_target_image_preserves_database_and_document_policies(self):
        root = Path(__file__).resolve().parents[1]
        path = "servers/truenas/apps/paperless-ngx/compose.yml"
        if ref := os.environ.get("HOMELAB_TEST_REF"):
            source = subprocess.check_output(["git", "show", f"{ref}:{path}"], cwd=root, text=True)
        else:
            source = (root / path).read_text()
        service = yaml.safe_load(source)["services"]["paperless"]
        variables = dict(value.split("=", 1) for value in service["environment"])
        command = ["docker", "run", "--rm", "--network", "none", "--pull", "never",
                   "--entrypoint", "python", "-e", "PAPERLESS_SECRET_KEY=isolated-test"]
        for key in ["PAPERLESS_DBENGINE", "PAPERLESS_DBHOST", "PAPERLESS_OCR_MODE",
                    "PAPERLESS_ARCHIVE_FILE_GENERATION", "PAPERLESS_CONSUMER_DELETE_DUPLICATES"]:
            if key in variables:
                command += ["-e", f"{key}={variables[key]}"]
        # Pin the upgrade target so the same check can expose v2 compose defaults.
        command += ["ghcr.io/paperless-ngx/paperless-ngx:3.5.0", "-c",
                    "from paperless import settings as s; import json; "
                    "print(json.dumps([s.DATABASES['default']['ENGINE'], s.OCR_MODE, "
                    "s.ARCHIVE_FILE_GENERATION, s.CONSUMER_DELETE_DUPLICATES]))"]
        result = subprocess.check_output(command, text=True, timeout=60)
        self.assertEqual(json.loads(result), ["django.db.backends.postgresql", "auto", "always", True])


if __name__ == "__main__":
    unittest.main()
