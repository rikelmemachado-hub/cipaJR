import concurrent.futures
import http.cookiejar
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import server


class ElectionApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary_directory = tempfile.TemporaryDirectory()
        server.DATABASE = Path(cls.temporary_directory.name) / "election.sqlite3"
        server.ADMIN_EMAIL = "admin@example.test"
        server.ADMIN_PASSWORD = "test-password-that-is-long-enough"
        server.PUBLIC_ORIGIN = ""
        server.COOKIE_SECURE = False
        server.initialize_database()
        cls.http_server = server.ThreadingHTTPServer(("127.0.0.1", 0), server.ElectionHandler)
        cls.thread = threading.Thread(target=cls.http_server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = f"http://127.0.0.1:{cls.http_server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.http_server.shutdown()
        cls.http_server.server_close()
        cls.thread.join()
        cls.temporary_directory.cleanup()

    def setUp(self):
        with server.connect_db() as database:
            database.execute("DELETE FROM ballots")
            database.execute("DELETE FROM candidates")
            database.execute("DELETE FROM employees")
            database.execute("DELETE FROM admin_audit")
            database.execute(
                "UPDATE election_state SET current_election_id = 1 WHERE id = 1"
            )
            database.execute("DELETE FROM elections WHERE id != 1")
            database.execute("UPDATE elections SET status = 'draft' WHERE id = 1")
        with server._sessions_lock:
            server._sessions.clear()
        with server._login_attempts_lock:
            server._login_attempts.clear()
        self.cookies = http.cookiejar.CookieJar()
        self.client = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies)
        )

    def request(self, method, path, payload=None, cookie=None, origin=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.origin}{path}",
            data=body,
            method=method,
            headers={
                **({"Content-Type": "application/json"} if body is not None else {}),
                **({"Origin": origin or self.origin} if method in ("POST", "DELETE") else {}),
                **({"Cookie": cookie} if cookie else {}),
            },
        )
        opener = self.client if cookie is None else urllib.request.build_opener()
        try:
            response = opener.open(request)
        except urllib.error.HTTPError as error:
            response = error
        body = response.read().decode("utf-8")
        if response.headers.get_content_type() == "application/json":
            body = json.loads(body)
        return response.code, body, response.headers

    def login_admin(self):
        status, _, _ = self.request(
            "POST",
            "/api/login",
            {
                "role": "admin",
                "email": server.ADMIN_EMAIL,
                "password": server.ADMIN_PASSWORD,
            },
        )
        self.assertEqual(status, 200)
        cookie = next(cookie.value for cookie in self.cookies if cookie.name == "cipa_session")
        return f"cipa_session={cookie}"

    def test_vote_is_single_anonymous_and_results_appear_after_close(self):
        admin_cookie = self.login_admin()
        status, employee_one, _ = self.request(
            "POST",
            "/api/admin/employees",
            {"name": "Ana Silva", "registration": "1001", "sector": "Produção"},
            cookie=admin_cookie,
        )
        self.assertEqual(status, 201)
        status, employee_two, _ = self.request(
            "POST",
            "/api/admin/employees",
            {"name": "Beto Souza", "registration": "1002", "sector": "Qualidade"},
            cookie=admin_cookie,
        )
        self.assertEqual(status, 201)
        status, candidate, _ = self.request(
            "POST",
            "/api/admin/candidates",
            {"name": "Carla Lima", "role": "Operadora", "pitch": "Mais prevenção"},
            cookie=admin_cookie,
        )
        self.assertEqual(status, 201)
        self.assertNotIn("codeHash", employee_one)

        status, _, _ = self.request(
            "POST", "/api/admin/election/open", {}, cookie=admin_cookie
        )
        self.assertEqual(status, 200)

        status, _, login_headers = self.request(
            "POST",
            "/api/login",
            {
                "role": "employee",
                "registration": "1001",
                "code": employee_one["code"],
            },
        )
        self.assertEqual(status, 200)
        first_cookie = login_headers["Set-Cookie"].split(";", 1)[0]

        def cast_vote():
            return self.request(
                "POST",
                "/api/vote",
                {"candidateId": candidate["id"]},
                cookie=first_cookie,
            )[0]

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda _index: cast_vote(), range(2)))
        self.assertCountEqual(statuses, [201, 409])

        status, _, _ = self.request(
            "POST",
            "/api/login",
            {
                "role": "employee",
                "registration": "1002",
                "code": employee_two["code"],
            },
        )
        self.assertEqual(status, 200)
        second_cookie = next(cookie.value for cookie in self.cookies if cookie.name == "cipa_session")
        status, _, _ = self.request(
            "POST", "/api/vote", {"candidateId": None}, cookie=f"cipa_session={second_cookie}"
        )
        self.assertEqual(status, 201)

        status, overview, _ = self.request(
            "GET", "/api/admin/overview", cookie=admin_cookie
        )
        self.assertEqual(status, 200)
        self.assertIsNone(overview["results"])
        status, _, _ = self.request(
            "POST", "/api/admin/election/close", {}, cookie=admin_cookie
        )
        self.assertEqual(status, 200)
        status, overview, _ = self.request(
            "GET", "/api/admin/overview", cookie=admin_cookie
        )
        self.assertEqual(status, 200)
        self.assertEqual(overview["results"]["castVotes"], 2)
        self.assertEqual(overview["results"]["turnout"], 2)
        self.assertEqual(overview["results"]["blankVotes"], 1)
        self.assertEqual(overview["results"]["candidates"][0]["votes"], 1)

        with server.connect_db() as database:
            columns = {
                row["name"]
                for row in database.execute("PRAGMA table_info(ballots)").fetchall()
            }
            foreign_keys = database.execute("PRAGMA foreign_key_list(ballots)").fetchall()
            stored_code = database.execute(
                "SELECT code_hash FROM employees WHERE registration = '1001'"
            ).fetchone()["code_hash"]
        self.assertEqual(columns, {"id", "candidate_id", "cast_at", "election_id"})
        self.assertEqual(len(foreign_keys), 2)
        self.assertNotEqual(stored_code, employee_one["code"].encode())
        status, _, _ = self.request(
            "POST", "/api/admin/election/open", {}, cookie=admin_cookie
        )
        self.assertEqual(status, 409)

    def test_new_election_preserves_history_and_starts_with_empty_current_records(self):
        admin_cookie = self.login_admin()
        status, _, _ = self.request(
            "POST", "/api/admin/election/new", {}, cookie=admin_cookie
        )
        self.assertEqual(status, 409)
        status, employee, _ = self.request(
            "POST",
            "/api/admin/employees",
            {"name": "Ana Silva", "registration": "1001", "sector": "Produção"},
            cookie=admin_cookie,
        )
        self.assertEqual(status, 201)
        status, candidate, _ = self.request(
            "POST",
            "/api/admin/candidates",
            {"name": "Carla Lima", "role": "Operadora", "pitch": "Mais prevenção"},
            cookie=admin_cookie,
        )
        self.assertEqual(status, 201)
        status, _, _ = self.request(
            "POST", "/api/admin/election/open", {}, cookie=admin_cookie
        )
        self.assertEqual(status, 200)
        status, _, voter_headers = self.request(
            "POST",
            "/api/login",
            {
                "role": "employee",
                "registration": "1001",
                "code": employee["code"],
            },
        )
        self.assertEqual(status, 200)
        voter_cookie = voter_headers["Set-Cookie"].split(";", 1)[0]
        status, _, _ = self.request(
            "POST",
            "/api/vote",
            {"candidateId": candidate["id"]},
            cookie=voter_cookie,
        )
        self.assertEqual(status, 201)
        status, _, _ = self.request(
            "POST", "/api/admin/election/close", {}, cookie=admin_cookie
        )
        self.assertEqual(status, 200)

        status, new_election, _ = self.request(
            "POST", "/api/admin/election/new", {}, cookie=admin_cookie
        )
        self.assertEqual(status, 201)
        self.assertEqual(new_election["electionId"], 2)
        status, overview, _ = self.request(
            "GET", "/api/admin/overview", cookie=admin_cookie
        )
        self.assertEqual(status, 200)
        self.assertEqual(overview["electionId"], 2)
        self.assertEqual(overview["status"], "draft")
        self.assertEqual(overview["employees"], [])
        self.assertEqual(overview["candidates"], [])
        self.assertIsNone(overview["results"])
        self.assertEqual(len(overview["history"]), 1)
        self.assertEqual(overview["history"][0]["id"], 1)
        self.assertEqual(overview["history"][0]["results"]["castVotes"], 1)
        self.assertEqual(
            overview["history"][0]["results"]["candidates"][0]["votes"], 1
        )

        status, next_employee, _ = self.request(
            "POST",
            "/api/admin/employees",
            {"name": "Ana Silva", "registration": "1001", "sector": "Produção"},
            cookie=admin_cookie,
        )
        self.assertEqual(status, 201)
        status, next_candidate, _ = self.request(
            "POST",
            "/api/admin/candidates",
            {"name": "Carla Lima", "role": "Operadora", "pitch": "Mais prevenção"},
            cookie=admin_cookie,
        )
        self.assertEqual(status, 201)
        status, _, _ = self.request(
            "POST", "/api/admin/election/open", {}, cookie=admin_cookie
        )
        self.assertEqual(status, 200)
        status, _, _ = self.request(
            "POST",
            "/api/login",
            {
                "role": "employee",
                "registration": "1001",
                "code": next_employee["code"],
            },
        )
        self.assertEqual(status, 200)
        next_voter_cookie = next(
            cookie.value for cookie in self.cookies if cookie.name == "cipa_session"
        )
        status, _, _ = self.request(
            "POST",
            "/api/vote",
            {"candidateId": next_candidate["id"]},
            cookie=f"cipa_session={next_voter_cookie}",
        )
        self.assertEqual(status, 201)
        status, overview, _ = self.request(
            "GET", "/api/admin/overview", cookie=admin_cookie
        )
        self.assertEqual(status, 200)
        self.assertEqual(overview["history"][0]["results"]["castVotes"], 1)
        self.assertIsNone(overview["results"])

    def test_initialize_migrates_existing_single_election_database(self):
        original_database = server.DATABASE
        with tempfile.TemporaryDirectory() as directory:
            server.DATABASE = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(server.DATABASE)
            connection.executescript(
                """
                CREATE TABLE election (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    status TEXT NOT NULL CHECK (status IN ('draft', 'open', 'closed'))
                );
                INSERT INTO election (id, status) VALUES (1, 'closed');
                CREATE TABLE employees (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    registration TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    sector TEXT NOT NULL,
                    code_salt BLOB NOT NULL,
                    code_hash BLOB NOT NULL,
                    has_voted INTEGER NOT NULL DEFAULT 0 CHECK (has_voted IN (0, 1)),
                    created_at TEXT NOT NULL
                );
                INSERT INTO employees VALUES ('employee-1', 'Ana', '1001', 'Produção', X'01', X'02', 1, '2026-01-01');
                CREATE TABLE candidates (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    pitch TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                INSERT INTO candidates VALUES ('candidate-1', 'Beto', 'Técnico', 'Prevenção', '2026-01-01');
                CREATE TABLE ballots (
                    id TEXT PRIMARY KEY,
                    candidate_id TEXT REFERENCES candidates(id),
                    cast_at TEXT NOT NULL
                );
                INSERT INTO ballots VALUES ('ballot-1', 'candidate-1', '2026-01-01');
                CREATE TABLE admin_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                INSERT INTO admin_audit (action, detail, created_at)
                VALUES ('election_closed', 'closed', '2026-01-01');
                """
            )
            connection.close()
            try:
                server.initialize_database()
                with server.connect_db() as database:
                    election = server.current_election(database)
                    employee = database.execute(
                        "SELECT election_id, registration FROM employees WHERE id = 'employee-1'"
                    ).fetchone()
                    ballot = database.execute(
                        "SELECT election_id, candidate_id FROM ballots WHERE id = 'ballot-1'"
                    ).fetchone()
                self.assertEqual(election["status"], "closed")
                self.assertEqual(employee["election_id"], 1)
                self.assertEqual(employee["registration"], "1001")
                self.assertEqual(ballot["election_id"], 1)
                self.assertEqual(ballot["candidate_id"], "candidate-1")
            finally:
                server.DATABASE = original_database

    def test_administration_requires_login_and_same_origin(self):
        status, _, _ = self.request("GET", "/api/admin/overview")
        self.assertEqual(status, 401)
        status, _, _ = self.request(
            "POST",
            "/api/login",
            {
                "role": "admin",
                "email": server.ADMIN_EMAIL,
                "password": server.ADMIN_PASSWORD,
            },
            origin="https://attacker.example",
        )
        self.assertEqual(status, 403)
        admin_cookie = self.login_admin()
        status, _, _ = self.request(
            "POST",
            "/api/admin/election/open",
            {},
            cookie=admin_cookie,
        )
        self.assertEqual(status, 409)

    def test_first_admin_can_create_account_once_and_sign_in(self):
        original_database = server.DATABASE
        original_email = server.ADMIN_EMAIL
        original_password = server.ADMIN_PASSWORD
        server.DATABASE = Path(self.temporary_directory.name) / "unconfigured.sqlite3"
        server.ADMIN_EMAIL = ""
        server.ADMIN_PASSWORD = ""
        try:
            server.initialize_database()
            status, setup, _ = self.request("GET", "/api/setup")
            self.assertEqual(status, 200)
            self.assertTrue(setup["setupRequired"])

            password = "new-admin-password-long"
            status, result, _ = self.request(
                "POST",
                "/api/admin/setup",
                {
                    "name": "Nova Administradora",
                    "email": "Admin@Example.test",
                    "password": password,
                },
            )
            self.assertEqual(status, 201)
            self.assertEqual(result["name"], "Nova Administradora")
            status, session, _ = self.request("GET", "/api/session")
            self.assertEqual(status, 200)
            self.assertTrue(session["authenticated"])
            self.assertEqual(session["role"], "admin")

            status, setup, _ = self.request("GET", "/api/setup")
            self.assertFalse(setup["setupRequired"])
            status, _, _ = self.request(
                "POST",
                "/api/admin/setup",
                {
                    "name": "Another Admin",
                    "email": "other@example.test",
                    "password": "another-password-long-enough",
                },
            )
            self.assertEqual(status, 409)

            with server.connect_db() as database:
                account = database.execute(
                    "SELECT email, password_hash FROM admin_account WHERE id = 1"
                ).fetchone()
            self.assertEqual(account["email"], "admin@example.test")
            self.assertNotEqual(account["password_hash"], password.encode("utf-8"))

            status, _, _ = self.request("POST", "/api/logout", {})
            self.assertEqual(status, 200)
            status, _, _ = self.request(
                "POST",
                "/api/login",
                {
                    "role": "admin",
                    "email": "ADMIN@example.test",
                    "password": password,
                },
            )
            self.assertEqual(status, 200)
        finally:
            server.DATABASE = original_database
            server.ADMIN_EMAIL = original_email
            server.ADMIN_PASSWORD = original_password

    def test_browser_scripts_are_served_from_page_paths(self):
        for path in ("/acesso.js", "/index.js"):
            status, _, _ = self.request("GET", path)
            self.assertEqual(status, 200)
        status, _, _ = self.request("GET", "/js/acesso.js")
        self.assertEqual(status, 404)
        status, page, _ = self.request("GET", "/acesso.html")
        self.assertEqual(status, 200)
        self.assertIn('id="newElectionButton"', page)
        self.assertIn('id="adminSetupForm"', page)
        self.assertIn('id="adminSetupPasswordConfirm"', page)
        self.assertIn("Ajuda do administrador", page)
        self.assertIn("Ajuda do eleitor", page)


if __name__ == "__main__":
    unittest.main()
