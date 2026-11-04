import base64
import http.client
import json
import os
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from torrentkit import bencode
from torrentkit.seeder import LocalSeeder
from torrentkit.web import WebApp

from .helpers import build_content, make_torrent


def http_json(base, path, *, method="GET", payload=None, timeout=20):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        base + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    return json.loads(body.decode("utf-8")) if body else {}


def wait_for_job(base, job_id, timeout=40.0):
    deadline = time.time() + timeout
    job = None
    while time.time() < deadline:
        job = http_json(base, f"/api/jobs/{job_id}")["job"]
        if job["state"] in ("completed", "error", "stopped"):
            return job
        time.sleep(0.2)
    return job


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temp = tempfile.TemporaryDirectory()
        cls.root = cls._temp.name
        cls.content = os.path.join(cls.root, "content")
        os.makedirs(cls.content)
        cls.blobs = build_content(cls.content)
        cls.torrent = make_torrent(cls.content)
        cls.torrent_b64 = base64.b64encode(bencode.encode(cls.torrent.metainfo)).decode("ascii")

        cls.seeder = LocalSeeder(cls.torrent, cls.root, host="127.0.0.1", port=0)
        cls.seed_port = cls.seeder.start()

        cls.app = WebApp(os.path.join(cls.root, "downloads"), port=0).start()
        cls.base = cls.app.url.rstrip("/")

    @classmethod
    def tearDownClass(cls):
        cls.app.stop()
        cls.seeder.stop()
        cls._temp.cleanup()

    # -- static UI --------------------------------------------------------

    def test_index_and_static_assets_are_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as response:
            html = response.read().decode("utf-8")
        self.assertIn("torrentkit", html)
        self.assertIn("/static/app.js", html)
        self.assertIn("/static/style.css", html)

        with urllib.request.urlopen(self.base + "/static/app.js", timeout=10) as response:
            script = response.read()
        self.assertGreater(len(script), 1000)

        with urllib.request.urlopen(self.base + "/static/style.css", timeout=10) as response:
            style = response.read()
        self.assertGreater(len(style), 1000)

    def test_ignored_request_body_does_not_break_keep_alive(self):
        # /stop never parses its body; the server must still consume it, or
        # the next request on this connection is garbled or the link reset.
        connection = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=10)
        try:
            for _ in range(3):
                connection.request(
                    "POST",
                    "/api/jobs/0123456789ab/stop",
                    body=b"{}",
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 404)
                connection.request("GET", "/api/health")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertTrue(json.loads(response.read())["ok"])
        finally:
            connection.close()

    def test_second_instance_cannot_share_the_port(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(OSError) as context:
                WebApp(os.path.join(temporary, "downloads"), port=self.app.port)
            self.assertIn("already in use", str(context.exception))

    def test_health_endpoint(self):
        data = http_json(self.base, "/api/health")
        self.assertTrue(data["ok"])
        self.assertTrue(data["version"])
        self.assertTrue(data["download_dir"])
        self.assertTrue(data["allow_external_dirs"])

    def test_directory_browser_and_custom_save_dir(self):
        with tempfile.TemporaryDirectory() as temporary:
            outside = os.path.join(temporary, "custom-output")
            os.makedirs(outside)

            listing = http_json(
                self.base, "/api/fs?path=" + urllib.parse.quote(temporary)
            )
            self.assertTrue(listing["ok"])
            self.assertEqual(Path(listing["path"]), Path(temporary))
            self.assertIn("custom-output", [item["name"] for item in listing["dirs"]])

            created = http_json(
                self.base,
                "/api/fs/mkdir",
                method="POST",
                payload={"parent": temporary, "name": "made-by-ui"},
            )
            self.assertTrue(Path(created["path"]).is_dir())

            job = http_json(
                self.base,
                "/api/jobs",
                method="POST",
                payload={
                    "torrent": self.torrent_b64,
                    "peers": [f"127.0.0.1:{self.seed_port}"],
                    "save_dir": outside,
                },
            )["job"]
            self.assertIn(outside.lower(), job["save_path"].lower())
            job = wait_for_job(self.base, job["id"])
            self.assertEqual(job["state"], "completed", job)
            with open(os.path.join(outside, "content", "sub", "b.bin"), "rb") as handle:
                self.assertEqual(handle.read(), self.blobs["sub/b.bin"])

    def test_external_directory_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = WebApp(
                os.path.join(temporary, "root"),
                port=0,
                allow_external_dirs=False,
            ).start()
            try:
                base = app.url.rstrip("/")
                with self.assertRaises(urllib.error.HTTPError) as context:
                    http_json(
                        base,
                        "/api/jobs",
                        method="POST",
                        payload={
                            "torrent": self.torrent_b64,
                            "save_dir": os.path.join(temporary, "outside"),
                        },
                    )
                self.assertEqual(context.exception.code, 400)

                with self.assertRaises(urllib.error.HTTPError) as context:
                    http_json(
                        base,
                        "/api/jobs",
                        method="POST",
                        payload={"torrent": self.torrent_b64, "save_dir": "relative/path"},
                    )
                self.assertEqual(context.exception.code, 400)
            finally:
                app.stop()

    def test_api_requires_json_content_type(self):
        body = json.dumps({"magnet": self.torrent.magnet_uri()}).encode("utf-8")
        request = urllib.request.Request(
            self.base + "/api/parse",
            data=body,
            headers={"Content-Type": "text/plain"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(context.exception.code, 415)

    # -- parsing ----------------------------------------------------------

    def test_parse_torrent(self):
        data = http_json(self.base, "/api/parse", method="POST", payload={"torrent": self.torrent_b64})
        self.assertEqual(data["source"], "torrent")
        self.assertEqual(data["name"], "content")
        self.assertEqual(data["info_hash"], self.torrent.info_hash.hex())
        self.assertEqual(data["files_total"], len(self.torrent.files))
        self.assertEqual(data["total_length"], self.torrent.total_length)
        self.assertIn("magnet:?xt=urn:btih:" + self.torrent.info_hash.hex(), data["magnet"])
        self.assertFalse(data["v2_only"])

    def test_parse_magnet(self):
        magnet = self.torrent.magnet_uri()
        data = http_json(self.base, "/api/parse", method="POST", payload={"magnet": magnet})
        self.assertEqual(data["source"], "magnet")
        self.assertFalse(data["resolved"])
        self.assertEqual(data["info_hash"], self.torrent.info_hash.hex())

    def test_invalid_torrent_payload_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as context:
            http_json(self.base, "/api/parse", method="POST", payload={"torrent": "%%%"})
        self.assertEqual(context.exception.code, 400)

    def test_unknown_job_returns_404(self):
        with self.assertRaises(urllib.error.HTTPError) as context:
            http_json(self.base, "/api/jobs/deadbeef")
        self.assertEqual(context.exception.code, 404)

    # -- jobs -------------------------------------------------------------

    def test_download_job_completes_and_can_be_removed(self):
        data = http_json(
            self.base,
            "/api/jobs",
            method="POST",
            payload={
                "torrent": self.torrent_b64,
                "peers": [f"127.0.0.1:{self.seed_port}"],
                "max_peers": 8,
            },
        )
        job = data["job"]
        job = wait_for_job(self.base, job["id"])
        self.assertEqual(job["state"], "completed", job)
        self.assertAlmostEqual(job["progress"], 1.0)
        self.assertEqual(job["downloaded"], self.torrent.total_length)
        self.assertEqual(job["pieces_done"], self.torrent.piece_count)

        output = Path(self.app.root) / "content"
        for relative, blob in self.blobs.items():
            with open(output / relative, "rb") as handle:
                self.assertEqual(handle.read(), blob)

        removed = http_json(self.base, f"/api/jobs/{job['id']}", method="DELETE")
        self.assertTrue(removed["ok"])
        jobs = http_json(self.base, "/api/jobs")["jobs"]
        self.assertNotIn(job["id"], [item["id"] for item in jobs])

    def test_magnet_job_resolves_metadata_then_downloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = WebApp(os.path.join(temporary, "downloads"), port=0).start()
            try:
                base = app.url.rstrip("/")
                payload = {
                    "magnet": self.torrent.magnet_uri(),
                    "peers": [f"127.0.0.1:{self.seed_port}"],
                }
                job = http_json(base, "/api/jobs", method="POST", payload=payload)["job"]
                job = wait_for_job(base, job["id"])
                self.assertEqual(job["state"], "completed", job)
                self.assertEqual(job["info_hash"], self.torrent.info_hash.hex())
                output = Path(app.root) / "content"
                with open(output / "sub" / "b.bin", "rb") as handle:
                    self.assertEqual(handle.read(), self.blobs["sub/b.bin"])
            finally:
                app.stop()

    def test_job_can_be_stopped(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = WebApp(os.path.join(temporary, "downloads"), port=0).start()
            try:
                base = app.url.rstrip("/")
                job = http_json(
                    base, "/api/jobs", method="POST", payload={"torrent": self.torrent_b64, "peers": ["127.0.0.1:1"]}
                )["job"]
                time.sleep(0.3)
                stopped = http_json(
                    base, f"/api/jobs/{job['id']}/stop", method="POST", payload={}
                )["job"]
                self.assertIn(stopped["state"], ("preparing", "verifying", "downloading", "stopped"))
                job = wait_for_job(base, job["id"], timeout=20)
                self.assertEqual(job["state"], "stopped", job)
            finally:
                app.stop()

    def test_job_without_peer_sources_fails_fast(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = WebApp(os.path.join(temporary, "downloads"), port=0).start()
            try:
                base = app.url.rstrip("/")
                job = http_json(
                    base, "/api/jobs", method="POST", payload={"torrent": self.torrent_b64}
                )["job"]
                job = wait_for_job(base, job["id"], timeout=10)
                self.assertEqual(job["state"], "downloading", job)
                self.assertEqual(job["peers"], 0)
                http_json(base, f"/api/jobs/{job['id']}/stop", method="POST", payload={})
                job = wait_for_job(base, job["id"], timeout=10)
                self.assertEqual(job["state"], "stopped", job)
            finally:
                app.stop()

    def test_duplicate_torrent_job_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = WebApp(os.path.join(temporary, "downloads"), port=0).start()
            try:
                base = app.url.rstrip("/")
                payload = {"torrent": self.torrent_b64, "peers": ["127.0.0.1:1"]}
                first = http_json(base, "/api/jobs", method="POST", payload=payload)["job"]
                with self.assertRaises(urllib.error.HTTPError) as context:
                    http_json(base, "/api/jobs", method="POST", payload=payload)
                self.assertEqual(context.exception.code, 409)
                http_json(base, f"/api/jobs/{first['id']}/stop", method="POST", payload={})
                wait_for_job(base, first["id"], timeout=20)
            finally:
                app.stop()

    def test_queue_limits_concurrency_and_starts_next_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = WebApp(os.path.join(temporary, "downloads"), port=0, max_active=1).start()
            try:
                base = app.url.rstrip("/")
                content = os.path.join(temporary, "second-content")
                os.makedirs(content)
                build_content(content)
                second_torrent = make_torrent(content)
                second_b64 = base64.b64encode(
                    bencode.encode(second_torrent.metainfo)
                ).decode("ascii")

                first = http_json(
                    base, "/api/jobs", method="POST", payload={"torrent": self.torrent_b64, "peers": ["127.0.0.1:1"]}
                )["job"]
                second = http_json(
                    base, "/api/jobs", method="POST", payload={"torrent": second_b64, "peers": ["127.0.0.1:1"]}
                )["job"]
                self.assertEqual(second["state"], "queued")
                self.assertEqual(second["queue_position"], 1)
                self.assertEqual(second["prepare_done"], 0)

                http_json(base, f"/api/jobs/{first['id']}/stop", method="POST", payload={})
                deadline = time.time() + 10
                state = "queued"
                while time.time() < deadline:
                    state = http_json(base, f"/api/jobs/{second['id']}")["job"]["state"]
                    if state != "queued":
                        break
                    time.sleep(0.1)
                self.assertNotEqual(state, "queued")

                http_json(base, f"/api/jobs/{second['id']}/stop", method="POST", payload={})
                stopped = wait_for_job(base, second["id"], timeout=20)
                self.assertEqual(stopped["state"], "stopped", stopped)
            finally:
                app.stop()

    def test_queued_job_can_be_stopped_before_it_starts(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = WebApp(os.path.join(temporary, "downloads"), port=0, max_active=1).start()
            try:
                base = app.url.rstrip("/")
                content = os.path.join(temporary, "second-content")
                os.makedirs(content)
                build_content(content)
                second_torrent = make_torrent(content)
                second_b64 = base64.b64encode(
                    bencode.encode(second_torrent.metainfo)
                ).decode("ascii")

                first = http_json(
                    base, "/api/jobs", method="POST", payload={"torrent": self.torrent_b64, "peers": ["127.0.0.1:1"]}
                )["job"]
                second = http_json(
                    base, "/api/jobs", method="POST", payload={"torrent": second_b64, "peers": ["127.0.0.1:1"]}
                )["job"]
                self.assertEqual(second["state"], "queued")

                stopped = http_json(
                    base, f"/api/jobs/{second['id']}/stop", method="POST", payload={}
                )["job"]
                self.assertEqual(stopped["state"], "stopped")
                self.assertIsNotNone(stopped["finished_at"])

                http_json(base, f"/api/jobs/{first['id']}/stop", method="POST", payload={})
                wait_for_job(base, first["id"], timeout=20)
            finally:
                app.stop()


if __name__ == "__main__":
    unittest.main()
