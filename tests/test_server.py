import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
import app  # noqa: E402

PASS = "correct horse battery"
UNIVERSE = "Company Name,Industry,Symbol,Series,ISIN Code\nAaa Ltd,Banks,AAA,EQ,X\nBbb Ltd,IT,BBB,EQ,Y\n"
BHAV = ("SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, "
        "TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER\n"
        "AAA, EQ, 01-Oct-2026, 100, 100, 100, 100, 100, 110, 100, 5000, 55.0, 10, 2000, 40.0\n")


class Client:
    def __init__(self, base):
        self.base, self.cookie = base, None

    def call(self, method, path, body=None, headers=None, raw=None):
        h = {"X-App": "1", **(headers or {})}
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if body is not None:
            h["Content-Type"] = "application/json"
        if self.cookie:
            h["Cookie"] = self.cookie
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                status, payload, hdrs = r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            status, payload, hdrs = e.code, e.read(), e.headers
        if "Set-Cookie" in hdrs and "Max-Age=0" not in hdrs["Set-Cookie"]:
            self.cookie = hdrs["Set-Cookie"].split(";")[0]
        try:
            return status, json.loads(payload)
        except ValueError:
            return status, payload

    def login(self):
        return self.call("POST", "/api/login", {"passcode": PASS})


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = app.Config(passcode=PASS, data_dir=Path(cls.tmp.name), refresh=False)
        cls.server = app.make_server(cfg, "127.0.0.1", 0)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.server.throttle = app.Throttle()
        self.c = Client(self.base)

    def authed(self):
        self.assertEqual(self.c.login()[0], 200)
        return self.c

    # ---- static + health

    def test_health_and_static(self):
        self.assertEqual(self.c.call("GET", "/healthz")[0], 200)
        status, body = self.c.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Daily 10", body)
        status, data = self.c.call("GET", "/data/latest.json")  # demo file until real data exists
        self.assertEqual(status, 200)
        self.assertTrue(data["demo"])

    def test_path_traversal_is_blocked(self):
        for path in ("/../server/db.py", "/..%2fserver%2fdb.py", "/%2e%2e/README.md", "/%00"):
            self.assertEqual(self.c.call("GET", path)[0], 404, path)

    def test_security_headers(self):
        req = urllib.request.Request(self.base + "/")
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])

    # ---- auth

    def test_api_requires_login(self):
        self.assertEqual(self.c.call("GET", "/api/state")[0], 401)
        self.assertEqual(self.c.call("POST", "/api/picks", {"cycleId": 1, "symbol": "AAA", "pickedOn": "2026-10-27"})[0], 401)
        self.assertFalse(self.c.call("GET", "/api/session")[1]["authed"])

    def test_wrong_passcode_and_throttle(self):
        for _ in range(5):
            self.assertEqual(self.c.call("POST", "/api/login", {"passcode": "nope-nope"})[0], 401)
        self.assertEqual(self.c.call("POST", "/api/login", {"passcode": "nope-nope"})[0], 429)
        self.assertEqual(self.c.login()[0], 429)  # even the right one, until the window passes

    def test_login_cookie_flags_and_session(self):
        req = urllib.request.Request(self.base + "/api/login", data=json.dumps({"passcode": PASS}).encode(),
                                     headers={"Content-Type": "application/json", "X-Forwarded-Proto": "https"}, method="POST")
        with urllib.request.urlopen(req) as r:
            cookie = r.headers["Set-Cookie"]
        for flag in ("HttpOnly", "SameSite=Strict", "Secure"):
            self.assertIn(flag, cookie)
        self.c.login()
        self.assertTrue(self.c.call("GET", "/api/session")[1]["authed"])
        self.c.call("POST", "/api/logout")
        self.c.cookie = None
        self.assertFalse(self.c.call("GET", "/api/session")[1]["authed"])

    def test_tampered_cookie_rejected(self):
        self.c.login()
        exp, _, sig = self.c.cookie.split("=", 1)[1].partition(".")
        self.c.cookie = f"d10={int(exp) + 999}.{sig}"
        self.assertEqual(self.c.call("GET", "/api/state")[0], 401)

    def test_mutations_need_x_app_header(self):
        self.c.login()
        req = urllib.request.Request(self.base + "/api/cycles", data=b"", method="POST", headers={"Cookie": self.c.cookie})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 403)

    # ---- the buy / sell flow

    def test_buy_partial_sell_and_undo_over_http(self):
        c = self.authed()
        state = c.call("GET", "/api/state")[1]
        cycle = state["cycles"][-1]["id"]
        pid = c.call("POST", "/api/picks", {"cycleId": cycle, "symbol": "ZZZ", "pickedOn": "2026-10-27"})[1]["id"]
        self.assertEqual(c.call("POST", "/api/picks", {"cycleId": cycle, "symbol": "ZZZ", "pickedOn": "2026-10-27"})[0], 409)
        self.assertEqual(c.call("PUT", f"/api/picks/{pid}/invest", {"entryPrice": 50, "qty": 100, "investedOn": "2026-10-27"})[0], 200)
        self.assertEqual(c.call("POST", f"/api/picks/{pid}/sells", {"qty": 150, "price": 60, "soldOn": "2026-10-28"})[0], 400)
        sid = c.call("POST", f"/api/picks/{pid}/sells", {"qty": 40, "price": 60, "soldOn": "2026-10-28"})[1]["id"]
        pick = next(p for p in c.call("GET", "/api/state")[1]["picks"] if p["id"] == pid)
        self.assertEqual(pick["sells"], [{"id": sid, "qty": 40, "price": 60.0, "soldOn": "2026-10-28"}])
        self.assertEqual(c.call("DELETE", f"/api/picks/{pid}")[0], 409)
        self.assertEqual(c.call("DELETE", f"/api/sells/{sid}")[0], 200)
        self.assertEqual(c.call("DELETE", f"/api/picks/{pid}")[0], 200)

    def test_ignore_and_sell_by_symbol_over_http(self):
        c = self.authed()
        cycle = c.call("GET", "/api/state")[1]["cycles"][-1]["id"]
        pid = c.call("POST", "/api/picks", {"cycleId": cycle, "symbol": "M&M", "pickedOn": "2026-10-27"})[1]["id"]
        self.assertEqual(c.call("POST", f"/api/picks/{pid}/ignore")[0], 200)
        get = lambda: next(p for p in c.call("GET", "/api/state")[1]["picks"] if p["id"] == pid)  # noqa: E731
        self.assertEqual(get()["status"], "ignored")
        self.assertEqual(c.call("DELETE", f"/api/picks/{pid}/ignore")[0], 200)
        self.assertEqual(get()["status"], "picked")
        c.call("PUT", f"/api/picks/{pid}/invest", {"entryPrice": 100, "qty": 10, "investedOn": "2026-10-27"})
        self.assertEqual(c.call("POST", f"/api/picks/{pid}/ignore")[0], 409)
        status, res = c.call("POST", "/api/holdings/M%26M/sells", {"qty": 4, "price": 120, "soldOn": "2026-10-28"})
        self.assertEqual(status, 200, res)
        self.assertEqual([(s["pickId"], s["qty"]) for s in res["sells"]], [(pid, 4)])
        self.assertEqual(c.call("POST", "/api/holdings/M%26M/sells", {"qty": 7, "price": 120, "soldOn": "2026-10-28"})[0], 400)
        self.assertEqual(c.call("POST", "/api/holdings/M%26M/sells", {"qty": 6, "price": 120, "soldOn": "2026-10-28"})[0], 200)

    def test_bad_json_is_a_400_not_a_crash(self):
        c = self.authed()
        self.assertEqual(c.call("POST", "/api/picks", raw=b"{not json", headers={"Content-Type": "application/json"})[0], 400)
        self.assertEqual(c.call("POST", "/api/picks", raw=b"[1]", headers={"Content-Type": "application/json"})[0], 400)

    # ---- admin upload fallback

    def test_upload_universe_and_bhav_with_bearer(self):
        bearer = {"Authorization": f"Bearer {PASS}"}
        c = Client(self.base)
        self.assertEqual(c.call("POST", "/api/admin/upload?kind=bhav&date=2026-10-01", raw=BHAV.encode(), headers={"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(c.call("POST", "/api/admin/upload?kind=universe", raw=b"garbage", headers=bearer)[0], 400)
        self.assertEqual(c.call("POST", "/api/admin/upload?kind=universe", raw=UNIVERSE.encode(), headers=bearer)[0], 200)
        status, res = c.call("POST", "/api/admin/upload?kind=bhav&date=2026-10-01", raw=BHAV.encode(), headers=bearer)
        self.assertEqual(status, 200, res)
        data = c.call("GET", "/data/latest.json")[1]
        self.assertFalse(data["demo"])
        self.assertEqual(data["stocks"][0]["symbol"], "AAA")
        self.assertEqual(c.call("POST", "/api/admin/upload?kind=bhav&date=bad", raw=BHAV.encode(), headers=bearer)[0], 400)

        # market cap: bad file rejected, good file stores shares and rebuilds the list without network
        self.assertEqual(c.call("POST", "/api/admin/upload?kind=mcap", raw=b"Symbol\nAAA\n", headers=bearer)[0], 422)
        mcap = ("Symbol, Series, Issue Size, Close Price\nAAA, EQ, 1000000000, 110\n").encode()
        self.assertEqual(c.call("POST", "/api/admin/upload?kind=mcap", raw=mcap, headers=bearer)[0], 200)
        data = c.call("GET", "/data/latest.json")[1]
        self.assertTrue(data["mcap"]["loaded"])
        self.assertEqual(data["stocks"][0]["mcapCr"], 11000.0)  # 110 x 1e9 / 1e7


class ConfigTests(unittest.TestCase):
    def test_short_passcode_refuses_to_start(self):
        with unittest.mock.patch.dict(os.environ, {"APP_PASSCODE": "short"}, clear=True):
            with self.assertRaises(SystemExit):
                app.Config.from_env()

    def test_railway_without_volume_is_flagged_ephemeral(self):
        env = {"APP_PASSCODE": PASS, "RAILWAY_PROJECT_ID": "x"}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            self.assertTrue(app.Config.from_env().ephemeral)
        with unittest.mock.patch.dict(os.environ, {**env, "RAILWAY_VOLUME_MOUNT_PATH": "/data"}, clear=True):
            cfg = app.Config.from_env()
            self.assertFalse(cfg.ephemeral)
            self.assertEqual(cfg.data_dir, Path("/data"))


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
