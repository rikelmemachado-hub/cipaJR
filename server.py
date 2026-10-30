import hashlib
import hmac
import json
import os
import re
import secrets
import psycopg
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit
ROOT = Path(__file__).resolve().parent
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
HOST = os.environ.get("CIPA_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", os.environ.get("CIPA_PORT", "3000")))
ADMIN_EMAIL = os.environ.get("CIPA_ADMIN_EMAIL", "").strip().casefold()
ADMIN_PASSWORD = os.environ.get("CIPA_ADMIN_PASSWORD", "")
PUBLIC_ORIGIN = os.environ.get("CIPA_PUBLIC_ORIGIN", "").rstrip("/")
COOKIE_SECURE = os.environ.get("CIPA_COOKIE_SECURE", "").casefold() == "true"
PASSWORD_ITERATIONS = 210_000
SESSION_LIFETIME = 8 * 60 * 60
LOGIN_WINDOW = 15 * 60
MAX_LOGIN_ATTEMPTS = 5
_sessions = {}
_sessions_lock = threading.Lock()
_login_attempts = {}
_login_attempts_lock = threading.Lock()
_DUMMY_SALT = secrets.token_bytes(16)
_DUMMY_HASH = hashlib.pbkdf2_hmac(
    "sha256", b"invalid-login", _DUMMY_SALT, PASSWORD_ITERATIONS
)
class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
class HybridRow(dict):
    """Dict-like PostgreSQL row that also supports SQLite-style numeric indexes."""
    def __init__(self, names, values):
        super().__init__(zip(names, values))
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


def hybrid_row_factory(cursor):
    names = [column.name for column in cursor.description]
    return lambda values: HybridRow(names, values)


class DatabaseConnection:
    """Small compatibility layer for the existing SQL while using PostgreSQL."""
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self.connection.__exit__(exc_type, exc_value, traceback)

    def execute(self, sql, parameters=()):
        statement = sql.strip()
        if statement.upper() == "BEGIN IMMEDIATE":
            # Lock the singleton election pointer to serialize election-changing actions.
            return self.connection.execute(
                "SELECT id FROM election_state WHERE id = 1 FOR UPDATE"
            )
        sql = sql.replace("?", "%s")
        sql = re.sub(r"\s+COLLATE\s+NOCASE", "", sql, flags=re.IGNORECASE)
        sql = sql.replace("has_voted = 0", "has_voted = FALSE")
        sql = sql.replace("has_voted = 1", "has_voted = TRUE")
        sql = sql.replace("SUM(has_voted)", "COUNT(*) FILTER (WHERE has_voted)")
        return self.connection.execute(sql, parameters)


def connect_db():
    if not DATABASE_URL:
        raise RuntimeError("A variável DATABASE_URL não foi configurada.")
    connection = psycopg.connect(
        DATABASE_URL,
        connect_timeout=15,
        row_factory=hybrid_row_factory,
        application_name="VotaCipa",
    )
    return DatabaseConnection(connection)


def initialize_database():
    """Check the Supabase schema and ensure the current election pointer exists."""
    with connect_db() as database:
        required_tables = {
            "elections", "election_state", "employees", "candidates",
            "ballots", "admin_audit", "admin_account",
        }
        rows = database.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = ANY(%s)",
            (list(required_tables),),
        ).fetchall()
        existing = {row["table_name"] for row in rows}
        missing = required_tables - existing
        if missing:
            raise RuntimeError(
                "Tabelas ausentes no Supabase: " + ", ".join(sorted(missing))
                + ". Confira o SQL de criação do banco."
            )

        state = database.execute(
            "SELECT current_election_id FROM election_state WHERE id = 1"
        ).fetchone()
        if state is None:
            election = database.execute(
                "INSERT INTO elections (status, created_at) VALUES ('draft', %s) RETURNING id",
                (now_utc(),),
            ).fetchone()
            database.execute(
                "INSERT INTO election_state (id, current_election_id) VALUES (1, %s)",
                (election["id"],),
            )

        if ADMIN_EMAIL and ADMIN_PASSWORD:
            account = database.execute(
                "SELECT id FROM admin_account WHERE id = 1"
            ).fetchone()
            if not account:
                salt = secrets.token_bytes(16)
                database.execute(
                    "INSERT INTO admin_account "
                    "(id, name, email, password_salt, password_hash, created_at) "
                    "VALUES (1, %s, %s, %s, %s, %s)",
                    ("Administrador", ADMIN_EMAIL, salt,
                     hash_password(ADMIN_PASSWORD, salt), now_utc()),
                )

def current_election(database):
    return database.execute(
        "SELECT e.id, e.status FROM elections e "
        "JOIN election_state s ON s.current_election_id = e.id WHERE s.id = 1"
    ).fetchone()
def election_results(database, election_id):
    candidates = database.execute(
        "SELECT c.id, c.name, COUNT(b.id) AS votes "
        "FROM candidates c LEFT JOIN ballots b "
        "ON b.candidate_id = c.id AND b.election_id = c.election_id "
        "WHERE c.election_id = ? "
        "GROUP BY c.id ORDER BY votes DESC, c.name COLLATE NOCASE",
        (election_id,),
    ).fetchall()
    blank_votes = database.execute(
        "SELECT COUNT(*) FROM ballots WHERE election_id = ? AND candidate_id IS NULL",
        (election_id,),
    ).fetchone()[0]
    cast_votes = database.execute(
        "SELECT COUNT(*) FROM ballots WHERE election_id = ?", (election_id,)
    ).fetchone()[0]
    employees = database.execute(
        "SELECT COUNT(*) AS eligible, "
        "COALESCE(SUM(has_voted), 0) AS turnout FROM employees WHERE election_id = ?",
        (election_id,),
    ).fetchone()
    return {
        "candidates": [dict(row) for row in candidates],
        "blankVotes": blank_votes,
        "castVotes": cast_votes,
        "eligibleVoters": employees["eligible"],
        "turnout": employees["turnout"],
    }
def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
def hash_code(code, salt):
    return hashlib.pbkdf2_hmac(
        "sha256", code.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
def hash_password(password, salt):
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
def new_id():
    return secrets.token_urlsafe(18)
def clean_text(value, label, maximum):
    if not isinstance(value, str):
        raise ApiError(400, f"Informe {label}.")
    value = value.strip()
    if not value or len(value) > maximum:
        raise ApiError(400, f"{label.capitalize()} deve ter entre 1 e {maximum} caracteres.")
    return value
def employee_payload(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "registration": row["registration"],
        "sector": row["sector"],
        "hasVoted": bool(row["has_voted"]),
    }
class ElectionHandler(BaseHTTPRequestHandler):
    server_version = "CipaElection"
    sys_version = ""
    def log_message(self, _format, *_args):
        return
    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' https://images.unsplash.com data:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        super().end_headers()
    def send_json(self, status, payload, extra_headers=()):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)
    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ApiError(400, "Requisição inválida.") from error
        if length < 1 or length > 16_384:
            raise ApiError(413, "Tamanho da requisição inválido.")
        if self.headers.get_content_type() != "application/json":
            raise ApiError(415, "Envie os dados em JSON.")
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ApiError(400, "JSON inválido.") from error
        if not isinstance(payload, dict):
            raise ApiError(400, "Formato de requisição inválido.")
        return payload
    def require_same_origin(self):
        origin = self.headers.get("Origin", "").rstrip("/")
        if PUBLIC_ORIGIN:
            expected = PUBLIC_ORIGIN
        else:
            scheme = "https" if COOKIE_SECURE else "http"
            host = self.headers.get("Host", "")
            expected = f"{scheme}://{host}"
        if not origin or not hmac.compare_digest(origin, expected):
            raise ApiError(403, "Origem da requisição não autorizada.")
    def get_session(self):
        cookie = self.headers.get("Cookie", "")
        token = ""
        for part in cookie.split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name == "cipa_session":
                token = value
                break
        if not token:
            return None
        now = time.time()
        with _sessions_lock:
            session = _sessions.get(token)
            if session and session["expires"] > now:
                return token, session
            _sessions.pop(token, None)
        return None
    def require_role(self, role):
        session = self.get_session()
        if not session or session[1]["role"] != role:
            raise ApiError(401, "Entre novamente para continuar.")
        return session[1]
    def cookie_header(self, token, max_age):
        secure = "; Secure" if COOKIE_SECURE else ""
        return (
            "Set-Cookie",
            f"cipa_session={token}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={max_age}{secure}",
        )
    def get_status(self):
        with connect_db() as database:
            return current_election(database)["status"]
    def handle_api_error(self, error):
        self.send_json(error.status, {"error": str(error)})
    def do_GET(self):
        path = urlsplit(self.path).path
        try:
            if path == "/api/setup":
                with connect_db() as database:
                    configured = database.execute(
                        "SELECT 1 FROM admin_account WHERE id = 1"
                    ).fetchone()
                self.send_json(200, {"setupRequired": configured is None})
                return
            if path == "/api/session":
                session = self.get_session()
                if not session:
                    self.send_json(200, {"authenticated": False})
                    return
                identity = session[1]
                result = {"authenticated": True, "role": identity["role"], "name": identity["name"]}
                if identity["role"] == "employee":
                    with connect_db() as database:
                        election_id = current_election(database)["id"]
                        row = database.execute(
                            "SELECT has_voted FROM employees WHERE id = ? AND election_id = ?",
                            (identity["id"], election_id),
                        ).fetchone()
                    result["hasVoted"] = bool(row["has_voted"]) if row else True
                self.send_json(200, result)
                return
            if path == "/api/election":
                with connect_db() as database:
                    election = current_election(database)
                    candidates = database.execute(
                        "SELECT id, name, role, pitch FROM candidates "
                        "WHERE election_id = ? ORDER BY name COLLATE NOCASE",
                        (election["id"],),
                    ).fetchall()
                self.send_json(
                    200,
                    {
                        "status": election["status"],
                        "candidates": [dict(candidate) for candidate in candidates],
                    },
                )
                return
            if path == "/api/admin/overview":
                self.require_role("admin")
                with connect_db() as database:
                    election = current_election(database)
                    employees = database.execute(
                        "SELECT id, name, registration, sector, has_voted "
                        "FROM employees WHERE election_id = ? ORDER BY name COLLATE NOCASE",
                        (election["id"],),
                    ).fetchall()
                    candidates = database.execute(
                        "SELECT id, name, role, pitch FROM candidates "
                        "WHERE election_id = ? ORDER BY name COLLATE NOCASE",
                        (election["id"],),
                    ).fetchall()
                    results = (
                        election_results(database, election["id"])
                        if election["status"] == "closed"
                        else None
                    )
                    history = database.execute(
                        "SELECT id, created_at FROM elections "
                        "WHERE id != ? AND status = 'closed' ORDER BY id DESC",
                        (election["id"],),
                    ).fetchall()
                    archived_elections = [
                        {
                            "id": row["id"],
                            "createdAt": row["created_at"],
                            "results": election_results(database, row["id"]),
                        }
                        for row in history
                    ]
                self.send_json(
                    200,
                    {
                        "electionId": election["id"],
                        "status": election["status"],
                        "employees": [employee_payload(row) for row in employees],
                        "candidates": [dict(candidate) for candidate in candidates],
                        "results": results,
                        "history": archived_elections,
                    },
                )
                return
            if path.startswith("/api/"):
                raise ApiError(404, "Recurso não encontrado.")
            self.serve_page(path)
        except ApiError as error:
            self.handle_api_error(error)
    def serve_page(self, path):
        pages = {
            "/": "acesso.html",
            "/index.html": "index.html",
            "/acesso.html": "acesso.html",
            "/index.js": "index.js",
            "/acesso.js": "acesso.js",
        }
        filename = pages.get(path)
        if not filename:
            self.send_error(404)
            return
        body = (ROOT / filename).read_bytes()
        self.send_response(200)
        content_type = (
            "text/javascript; charset=utf-8"
            if filename.endswith(".js")
            else "text/html; charset=utf-8"
        )
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_POST(self):
        try:
            path = urlsplit(self.path).path
            # Allow a local-only admin reset endpoint even without the same-origin check,
            # but only if explicitly enabled via environment variable.
            if path == "/api/admin/reset-local":
                payload = self.read_json()
                self.reset_local(payload)
                return
            # All other POST APIs require same-origin protection.
            self.require_same_origin()
            payload = self.read_json()
            if path == "/api/login":
                self.login(payload)
            elif path == "/api/admin/setup":
                self.setup_admin(payload)
            elif path == "/api/logout":
                self.logout()
            elif path == "/api/vote":
                self.vote(payload)
            elif path == "/api/admin/employees":
                self.create_employee(payload)
            elif path == "/api/admin/candidates":
                self.create_candidate(payload)
            elif path == "/api/admin/election/open":
                self.open_election()
            elif path == "/api/admin/election/close":
                self.close_election()
            elif path == "/api/admin/election/new":
                self.create_election()
            else:
                raise ApiError(404, "Recurso não encontrado.")
        except ApiError as error:
            self.handle_api_error(error)
    def do_DELETE(self):
        try:
            self.require_same_origin()
            path = unquote(urlsplit(self.path).path)
            if path.startswith("/api/admin/employees/"):
                self.delete_employee(path.rsplit("/", 1)[-1])
            elif path.startswith("/api/admin/candidates/"):
                self.delete_candidate(path.rsplit("/", 1)[-1])
            else:
                raise ApiError(404, "Recurso não encontrado.")
        except ApiError as error:
            self.handle_api_error(error)
    def login(self, payload):
        address = self.client_address[0]
        now = time.time()
        with _login_attempts_lock:
            attempts = [attempt for attempt in _login_attempts.get(address, []) if attempt > now - LOGIN_WINDOW]
            _login_attempts[address] = attempts
            if len(attempts) >= MAX_LOGIN_ATTEMPTS:
                raise ApiError(429, "Muitas tentativas. Aguarde 15 minutos.")
        role = payload.get("role")
        identity = None
        if role == "admin":
            email = payload.get("email", "")
            password = payload.get("password", "")
            if isinstance(email, str) and isinstance(password, str):
                normalized_email = email.strip().casefold()
                with connect_db() as database:
                    account = database.execute(
                        "SELECT name, email, password_salt, password_hash "
                        "FROM admin_account WHERE id = 1"
                    ).fetchone()
                salt = account["password_salt"] if account else _DUMMY_SALT
                expected_hash = account["password_hash"] if account else _DUMMY_HASH
                password_hash = hash_password(password, salt)
                stored_email = account["email"] if account else ""
                email_matches = hmac.compare_digest(
                    normalized_email.encode("utf-8"),
                    stored_email.casefold().encode("utf-8"),
                )
                if email_matches and hmac.compare_digest(password_hash, expected_hash):
                    identity = {"role": "admin", "name": account["name"]}
        elif role == "employee":
            registration = payload.get("registration", "")
            code = payload.get("code", "")
            if isinstance(registration, str) and isinstance(code, str):
                with connect_db() as database:
                    election_id = current_election(database)["id"]
                    employee = database.execute(
                        "SELECT id, name, code_salt, code_hash FROM employees "
                        "WHERE LOWER(registration) = LOWER(?) AND election_id = ?",
                        (registration.strip(), election_id),
                    ).fetchone()
                salt = employee["code_salt"] if employee else _DUMMY_SALT
                expected_hash = employee["code_hash"] if employee else _DUMMY_HASH
                if hmac.compare_digest(hash_code(code, salt), expected_hash) and employee:
                    identity = {
                        "role": "employee",
                        "id": employee["id"],
                        "name": employee["name"],
                    }
        if identity is None:
            with _login_attempts_lock:
                _login_attempts.setdefault(address, []).append(now)
            raise ApiError(401, "Acesso não reconhecido. Confira os dados e tente novamente.")
        with _login_attempts_lock:
            _login_attempts.pop(address, None)
        token = secrets.token_urlsafe(32)
        with _sessions_lock:
            _sessions[token] = {**identity, "expires": now + SESSION_LIFETIME}
        self.send_json(
            200,
            {"authenticated": True, "role": identity["role"], "name": identity["name"]},
            [self.cookie_header(token, SESSION_LIFETIME)],
        )
    def setup_admin(self, payload):
        name = clean_text(payload.get("name"), "nome", 100)
        email = clean_text(payload.get("email"), "e-mail", 254).casefold()
        password = payload.get("password", "")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+**\\.**[^@\s]+", email):
            raise ApiError(400, "Informe um e-mail válido.")
        if not isinstance(password, str) or len(password) < 14:
            raise ApiError(400, "A senha precisa ter pelo menos 14 caracteres.")
        if len(password) > 200:
            raise ApiError(400, "A senha deve ter no máximo 200 caracteres.")
        salt = secrets.token_bytes(16)
        with connect_db() as database:
            database.execute("BEGIN IMMEDIATE")
            if database.execute(
                "SELECT 1 FROM admin_account WHERE id = 1"
            ).fetchone():
                raise ApiError(409, "A conta de administrador já foi criada.")
            database.execute(
                "INSERT INTO admin_account "
                "(id, name, email, password_salt, password_hash, created_at) "
                "VALUES (1, ?, ?, ?, ?, ?)",
                (name, email, salt, hash_password(password, salt), now_utc()),
            )
            self.audit(database, "admin_created", email)
        token = secrets.token_urlsafe(32)
        with _sessions_lock:
            _sessions[token] = {
                "role": "admin",
                "name": name,
                "expires": time.time() + SESSION_LIFETIME,
            }
        self.send_json(
            201,
            {"authenticated": True, "role": "admin", "name": name},
            [self.cookie_header(token, SESSION_LIFETIME)],
        )
    def logout(self):
        session = self.get_session()
        if session:
            with _sessions_lock:
                _sessions.pop(session[0], None)
        self.send_json(200, {"authenticated": False}, [self.cookie_header("", 0)])
    def require_draft(self, database):
        if current_election(database)["status"] != "draft":
            raise ApiError(409, "Os cadastros não podem ser alterados após a abertura da eleição.")
    def audit(self, database, action, detail):
        election_id = current_election(database)["id"]
        database.execute(
            "INSERT INTO admin_audit (action, detail, created_at, election_id) "
            "VALUES (?, ?, ?, ?)",
            (action, detail, now_utc(), election_id),
        )
    def create_employee(self, payload):
        self.require_role("admin")
        name = clean_text(payload.get("name"), "nome", 80)
        registration = clean_text(payload.get("registration"), "matrícula", 30).upper()
        sector = clean_text(payload.get("sector"), "setor", 60)
        if not re.fullmatch(r"[A-Z0-9._-]{1,30}", registration):
            raise ApiError(400, "Matrícula aceita apenas letras, números, ponto, hífen e sublinhado.")
        code = secrets.token_urlsafe(9)
        salt = secrets.token_bytes(16)
        try:
            with connect_db() as database:
                database.execute("BEGIN IMMEDIATE")
                self.require_draft(database)
                election_id = current_election(database)["id"]
                employee_id = new_id()
                database.execute(
                    "INSERT INTO employees "
                    "(id, name, registration, sector, code_salt, code_hash, created_at, election_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        employee_id,
                        name,
                        registration,
                        sector,
                        salt,
                        hash_code(code, salt),
                        now_utc(),
                        election_id,
                    ),
                )
                self.audit(database, "employee_created", employee_id)
        except psycopg.IntegrityError as error:
            raise ApiError(409, "Já existe um funcionário com essa matrícula.") from error
        self.send_json(201, {"id": employee_id, "name": name, "registration": registration, "code": code})
    def create_candidate(self, payload):
        self.require_role("admin")
        name = clean_text(payload.get("name"), "nome", 80)
        role = clean_text(payload.get("role"), "cargo ou função", 80)
        pitch = clean_text(payload.get("pitch"), "proposta", 220)
        with connect_db() as database:
            database.execute("BEGIN IMMEDIATE")
            self.require_draft(database)
            election_id = current_election(database)["id"]
            candidate_id = new_id()
            database.execute(
                "INSERT INTO candidates (id, name, role, pitch, created_at, election_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (candidate_id, name, role, pitch, now_utc(), election_id),
            )
            self.audit(database, "candidate_created", candidate_id)
        self.send_json(201, {"id": candidate_id, "name": name, "role": role, "pitch": pitch})
    def reset_local(self, payload):
        """
        Local-only administrative reset of the admin email/password.
        Enabled only when environment variable CIPA_ALLOW_LOCAL_ADMIN_RESET=1.
        Requires the request to originate from localhost (127.0.0.1 or ::1).
        Payload JSON: { "email": "...", "password": "..." }
        Password must follow the same minimum-length rule as account setup (>=14).
        """
        # Only allow when explicitly enabled
        if os.environ.get("CIPA_ALLOW_LOCAL_ADMIN_RESET", "").strip().lower() != "1":
            raise ApiError(403, "Redefinição local de administrador desabilitada.")
        # Only accept requests from localhost
        client_ip = self.client_address[0]
        if client_ip not in ("127.0.0.1", "::1", "localhost"):
            raise ApiError(403, "Redefinição permitida apenas a partir do computador local.")
        email = payload.get("email", "")
        password = payload.get("password", "")
        if not isinstance(email, str) or not isinstance(password, str):
            raise ApiError(400, "Envie email e password em texto.")
        email = email.strip()
        email = email.casefold()
        if len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+**\\.**[^@\s]+", email):
            raise ApiError(400, "Informe um e-mail válido do administrador.")
        if len(password) < 14:
            raise ApiError(400, "A senha precisa ter ao menos 14 caracteres.")
        if len(password) > 200:
            raise ApiError(400, "A senha deve ter no máximo 200 caracteres.")
        salt = secrets.token_bytes(16)
        with connect_db() as database:
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                "INSERT INTO admin_account "
                "(id, name, email, password_salt, password_hash, created_at) "
                "VALUES (1, 'Administrador', ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET email = excluded.email, "
                "password_salt = excluded.password_salt, "
                "password_hash = excluded.password_hash",
                (email, salt, hash_password(password, salt), now_utc()),
            )
            self.audit(database, "admin_reset_local", f"reset_from_{client_ip}")
        self.send_json(200, {"reset": True})
    def delete_employee(self, employee_id):
        self.require_role("admin")
        with connect_db() as database:
            database.execute("BEGIN IMMEDIATE")
            self.require_draft(database)
            election_id = current_election(database)["id"]
            result = database.execute(
                "DELETE FROM employees WHERE id = ? AND election_id = ?",
                (employee_id, election_id),
            )
            if result.rowcount != 1:
                raise ApiError(404, "Funcionário não encontrado.")
            self.audit(database, "employee_deleted", employee_id)
        self.send_json(200, {"deleted": True})
    def delete_candidate(self, candidate_id):
        self.require_role("admin")
        with connect_db() as database:
            database.execute("BEGIN IMMEDIATE")
            self.require_draft(database)
            election_id = current_election(database)["id"]
            result = database.execute(
                "DELETE FROM candidates WHERE id = ? AND election_id = ?",
                (candidate_id, election_id),
            )
            if result.rowcount != 1:
                raise ApiError(404, "Candidato não encontrado.")
            self.audit(database, "candidate_deleted", candidate_id)
        self.send_json(200, {"deleted": True})
    def open_election(self):
        self.require_role("admin")
        with connect_db() as database:
            database.execute("BEGIN IMMEDIATE")
            self.require_draft(database)
            election_id = current_election(database)["id"]
            employees = database.execute(
                "SELECT COUNT(*) FROM employees WHERE election_id = ?", (election_id,)
            ).fetchone()[0]
            candidates = database.execute(
                "SELECT COUNT(*) FROM candidates WHERE election_id = ?", (election_id,)
            ).fetchone()[0]
            if employees < 1 or candidates < 1:
                raise ApiError(409, "Cadastre ao menos um funcionário e um candidato antes de abrir.")
            database.execute(
                "UPDATE elections SET status = 'open' WHERE id = ?", (election_id,)
            )
            self.audit(database, "election_opened", "open")
        self.send_json(200, {"status": "open"})
    def close_election(self):
        self.require_role("admin")
        with connect_db() as database:
            database.execute("BEGIN IMMEDIATE")
            election = current_election(database)
            if election["status"] != "open":
                raise ApiError(409, "A votação não está aberta.")
            database.execute(
                "UPDATE elections SET status = 'closed' WHERE id = ?", (election["id"],)
            )
            self.audit(database, "election_closed", "closed")
        self.send_json(200, {"status": "closed"})
    def create_election(self):
        self.require_role("admin")
        with connect_db() as database:
            database.execute("BEGIN IMMEDIATE")
            current = current_election(database)
            if current["status"] != "closed":
                raise ApiError(409, "Encerre a votação atual antes de iniciar outra eleição.")
            result = database.execute(
                "INSERT INTO elections (status, created_at) VALUES ('draft', ?) RETURNING id",
                (now_utc(),),
            )
            election_id = result.fetchone()["id"]
            database.execute(
                "UPDATE election_state SET current_election_id = ? WHERE id = 1",
                (election_id,),
            )
            self.audit(database, "election_created", str(election_id))
        self.send_json(201, {"electionId": election_id, "status": "draft"})
    def vote(self, payload):
        identity = self.require_role("employee")
        candidate_id = payload.get("candidateId")
        if candidate_id is not None and not isinstance(candidate_id, str):
            raise ApiError(400, "Escolha um candidato ou vote em branco.")
        with connect_db() as database:
            database.execute("BEGIN IMMEDIATE")
            election = current_election(database)
            election_id = election["id"]
            if election["status"] != "open":
                raise ApiError(409, "A votação não está aberta.")
            voter = database.execute(
                "SELECT has_voted FROM employees WHERE id = ? AND election_id = ?",
                (identity["id"], election_id),
            ).fetchone()
            if not voter:
                raise ApiError(401, "Cadastro de eleitor não encontrado.")
            if voter["has_voted"]:
                raise ApiError(409, "Este eleitor já registrou um voto.")
            if candidate_id is not None:
                candidate = database.execute(
                    "SELECT 1 FROM candidates WHERE id = ? AND election_id = ?",
                    (candidate_id, election_id),
                ).fetchone()
                if not candidate:
                    raise ApiError(400, "A candidatura escolhida não existe.")
            updated = database.execute(
                "UPDATE employees SET has_voted = 1 "
                "WHERE id = ? AND election_id = ? AND has_voted = 0",
                (identity["id"], election_id),
            )
            if updated.rowcount != 1:
                raise ApiError(409, "Este eleitor já registrou um voto.")
            database.execute(
                "INSERT INTO ballots (id, candidate_id, cast_at, election_id) "
                "VALUES (?, ?, ?, ?)",
                (new_id(), candidate_id, now_utc(), election_id),
            )
        self.send_json(201, {"registered": True, "protocol": f"CIPA-{secrets.token_hex(6).upper()}"})
def main():
    if not DATABASE_URL:
        raise SystemExit("Configure a variável DATABASE_URL no Render com a conexão do Supabase.")
    if bool(ADMIN_EMAIL) != bool(ADMIN_PASSWORD):
        raise SystemExit(
            "Configure CIPA_ADMIN_EMAIL e CIPA_ADMIN_PASSWORD juntos, ou crie a conta pela tela."
        )
    if ADMIN_EMAIL and not re.fullmatch(r"[^@\s]+@[^@\s]+**\\.**[^@\s]+", ADMIN_EMAIL):
        raise SystemExit("CIPA_ADMIN_EMAIL precisa ser um e-mail válido.")
    if ADMIN_PASSWORD and len(ADMIN_PASSWORD) < 14:
        raise SystemExit("CIPA_ADMIN_PASSWORD precisa ter pelo menos 14 caracteres.")
    if COOKIE_SECURE and not PUBLIC_ORIGIN.startswith("https://"):
        raise SystemExit("Com CIPA_COOKIE_SECURE=true, configure CIPA_PUBLIC_ORIGIN com HTTPS.")
    if PUBLIC_ORIGIN and (not PUBLIC_ORIGIN.startswith("https://") or not COOKIE_SECURE):
        raise SystemExit(
            "Uma origem pública exige CIPA_PUBLIC_ORIGIN HTTPS e CIPA_COOKIE_SECURE=true."
        )
    if HOST not in ("127.0.0.1", "localhost", "::1") and not COOKIE_SECURE:
        raise SystemExit(
            "O servidor só pode escutar fora do computador local com cookies Secure e HTTPS configurados."
        )
    initialize_database()
    server = ThreadingHTTPServer((HOST, PORT), ElectionHandler)
    server.daemon_threads = True
    print(f"Urna CIPA disponível em http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor encerrado.")
    finally:
        server.server_close()
if __name__ == "__main__":
    main()