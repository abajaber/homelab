"""Check the startup privilege boundary with isolated Docker volumes.

To run this network-dependent check, set HOMELAB_CONTAINER_TESTS=1 and run
`.venv/bin/python -m unittest discover -s tests` from the repository root.
"""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import uuid

import yaml


@unittest.skipUnless(os.environ.get("HOMELAB_CONTAINER_TESTS") == "1", "opt-in container check")
class MusicSyncStartupTest(unittest.TestCase):
    def test_dependencies_install_before_sync_runs_as_apps(self):
        root = Path(__file__).resolve().parents[1]
        compose_path = "servers/truenas/apps/music-tools/compose.yml"
        if ref := os.environ.get("HOMELAB_TEST_REF"):
            source = subprocess.check_output(["git", "show", f"{ref}:{compose_path}"], cwd=root, text=True)
        else:
            source = (root / compose_path).read_text()
        compose = yaml.safe_load(source)
        service = compose["services"]["music-sync"]
        name = "homelab-music-sync-test-" + uuid.uuid4().hex[:12]
        with tempfile.TemporaryDirectory(prefix="music-sync-", dir=os.environ.get("HOMELAB_TEST_TMPDIR")) as tmp:
            workspace = Path(tmp)
            workspace.chmod(0o755)
            for folder in ["config", "music", "scripts", "sources"]:
                (workspace / folder).mkdir(mode=0o777)
                (workspace / folder).chmod(0o777)
            for script in ["sync_yt", "sync_csv"]:
                (workspace / "scripts" / (script + ".py")).write_text(
                    "import json, os, pathlib, shutil\n"
                    f"pathlib.Path('/config/{script}.json').write_text(json.dumps({{\n"
                    " 'uid': os.getuid(), 'gid': os.getgid(),\n"
                    " 'ffmpeg': bool(shutil.which('ffmpeg')),\n"
                    " 'yt_dlp': bool(shutil.which('yt-dlp'))}))\n"
                )
            command = ["docker", "run", "-d", "--name", name, "--user", service["user"]]
            for folder in ["config", "music", "scripts", "sources"]:
                command += ["--volume", f"{workspace / folder}:/{folder}" + (":ro,Z" if folder in ["scripts", "sources"] else ":Z")]
            command += ["--entrypoint", service["entrypoint"][0], service["image"], *service["entrypoint"][1:]]
            try:
                subprocess.run(command, check=True, capture_output=True, text=True)
                deadline = time.monotonic() + 420
                while time.monotonic() < deadline:
                    probes = [workspace / "config" / (s + ".json") for s in ["sync_yt", "sync_csv"]]
                    if all(p.exists() for p in probes):
                        break
                    state = subprocess.check_output(["docker", "inspect", "--format", "{{.State.Running}}", name], text=True).strip()
                    if state != "true":
                        self.fail(subprocess.check_output(["docker", "logs", name], stderr=subprocess.STDOUT, text=True)[-3000:])
                    time.sleep(2)
                else:
                    self.fail("Startup did not reach the sync scripts within seven minutes")
                for probe in probes:
                    self.assertEqual(json.loads(probe.read_text()), {"uid": 568, "gid": 568, "ffmpeg": True, "yt_dlp": True})
                    self.assertEqual(probe.stat().st_uid, 568)
                for folder in [workspace / "config/archives", workspace / "config/logs", workspace / "music/playlists/_m3u"]:
                    self.assertEqual(folder.stat().st_uid, 568)
            finally:
                subprocess.run(["docker", "exec", "--user", "0", name, "chmod", "-R", "a+rwX", "/config", "/music"], capture_output=True)
                subprocess.run(["docker", "rm", "-f", name], capture_output=True)


if __name__ == "__main__":
    unittest.main()
