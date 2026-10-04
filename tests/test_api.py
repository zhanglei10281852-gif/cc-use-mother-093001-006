import json
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from water_balance.api import create_server


class ApiTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp.close()
        self.db_path = tmp.name
        self.server = create_server(self.db_path, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _req(self, method, path, payload=None, user=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        if user:
            headers["X-User-Id"] = user
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_end_to_end_over_http(self):
        status, _ = self._req("POST", "/users",
                              {"user_id": "admin1", "name": "A",
                               "roles": ["admin"]})
        self.assertEqual(status, 200)
        for uid, role in [("op", "operator"), ("rev", "reviewer"),
                          ("iss", "issuer")]:
            s, _ = self._req("POST", "/users",
                             {"user_id": uid, "name": uid, "roles": [role]},
                             "admin1")
            self.assertEqual(s, 200)

        self.assertEqual(self._req("POST", "/zones",
                                   {"zone_id": "Z", "name": "z"}, "op")[0], 200)
        self.assertEqual(self._req("POST", "/service-points", {
            "sp_id": "SPM", "zone_id": "Z",
            "valid_from": "2026-01-01"}, "op")[0], 200)
        self.assertEqual(self._req("POST", "/meters", {
            "meter_id": "MM", "sp_id": "SPM", "role": "master",
            "valid_from": "2026-01-01"}, "op")[0], 200)
        self.assertEqual(self._req("POST", "/readings", {
            "meter_id": "MM", "read_on": "2026-09-01", "value": 0.0},
            "op")[0], 200)
        self.assertEqual(self._req("POST", "/readings", {
            "meter_id": "MM", "read_on": "2026-10-01", "value": 50.0},
            "op")[0], 200)

        # 鉴权
        status, body = self._req("POST", "/recompute",
                                 {"zone_id": "Z", "month": "2026-09"})
        self.assertEqual(status, 400)
        # 复核与签发链路
        status, v = self._req("POST", "/recompute",
                              {"zone_id": "Z", "month": "2026-09"}, "op")
        self.assertEqual(status, 200)
        vid = v["version_id"]
        self.assertEqual(self._req("POST", f"/versions/{vid}/review",
                                   {}, "rev")[0], 200)
        self.assertEqual(self._req("POST", f"/versions/{vid}/issue",
                                   {}, "iss")[0], 200)
        # 只读接口
        status, versions = self._req(
            "GET", "/versions?zone=Z&month=2026-09")
        self.assertEqual(status, 200)
        self.assertEqual(versions[0]["state"], "issued")
        status, report = self._req("GET", "/verify?month=2026-09")
        self.assertEqual(status, 200)
        self.assertTrue(report["identity_ok"])


if __name__ == "__main__":
    unittest.main()
