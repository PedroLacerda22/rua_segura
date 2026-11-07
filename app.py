"""
Rua Segura - MVP para sinalizar problemas em ruas, calçadas e ciclovias.

Como executar:
    python app.py

Depois abra:
    http://localhost:8000        -> tela do cidadão (sinalizar problemas)
    http://localhost:8000/admin  -> painel da prefeitura (protegido por PIN)

Não precisa instalar nada além do Python 3.9+ (usa apenas a biblioteca padrão).

Variáveis de ambiente opcionais:
    ADMIN_PIN  PIN do painel da prefeitura (se vazio, um PIN temporário é gerado a cada execução)
    HOST       endereço de escuta (padrão 127.0.0.1; use 0.0.0.0 para acessar pelo celular na mesma rede)
    PORT       porta (padrão 8000)
    MAP_LAT / MAP_LNG  centro inicial do mapa (padrão: Rio de Janeiro)
"""

import base64
import binascii
import csv
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DB_PATH = Path(os.environ.get("DB_PATH", str(BASE_DIR / "ocorrencias.db")))
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
ADMIN_PIN = os.environ.get("ADMIN_PIN", "")
MAP_CENTER = [float(os.environ.get("MAP_LAT", "-22.9068")), float(os.environ.get("MAP_LNG", "-43.1729"))]
MAP_ZOOM = 13

MAX_BODY_BYTES = 4 * 1024 * 1024   # tamanho máximo de uma requisição
MAX_PHOTO_BYTES = 2 * 1024 * 1024  # tamanho máximo da foto já decodificada
MAX_DESCRIPTION = 500
MAX_NOTE = 1000

CATEGORIES = {
    "buraco": "Buraco no asfalto",
    "semaforo": "Semáforo queimado / com defeito",
    "iluminacao": "Iluminação pública apagada",
    "bueiro": "Bueiro aberto ou entupido",
    "sinalizacao": "Sinalização danificada ou ausente",
    "obstaculo_ciclovia": "Obstáculo na ciclovia",
    "arvore": "Árvore ou galho caído",
    "alagamento": "Alagamento",
    "outro": "Outro",
}
LOCATION_TYPES = {"rua": "Rua", "ciclovia": "Ciclovia", "calcada": "Calçada"}
STATUSES = {"aberto": "Aberto", "em_andamento": "Em andamento", "resolvido": "Resolvido"}

STATIC_PAGES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/admin": "admin.html",
    "/admin.html": "admin.html",
}


# --------------------------------------------------------------------------- banco de dados

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        with conn:  # commit automático (ou rollback em caso de erro)
            yield conn
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                category      TEXT NOT NULL,
                location_type TEXT NOT NULL,
                description   TEXT NOT NULL DEFAULT '',
                lat           REAL NOT NULL,
                lng           REAL NOT NULL,
                photo         BLOB,
                status        TEXT NOT NULL DEFAULT 'aberto',
                staff_note    TEXT NOT NULL DEFAULT '',
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status)")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


ORDER_BY = (
    "ORDER BY CASE status WHEN 'aberto' THEN 0 WHEN 'em_andamento' THEN 1 ELSE 2 END, "
    "created_at DESC"
)


def list_public_reports():
    # A visão pública NÃO inclui descrição, foto nem observações internas,
    # para evitar exposição de dados pessoais que alguém possa ter digitado/fotografado.
    with db() as conn:
        rows = conn.execute(
            f"SELECT id, category, location_type, lat, lng, status, created_at FROM reports {ORDER_BY}"
        ).fetchall()
    return [dict(r) for r in rows]


def list_admin_reports():
    with db() as conn:
        rows = conn.execute(
            "SELECT id, category, location_type, description, lat, lng, status, staff_note, "
            f"created_at, updated_at, photo IS NOT NULL AS has_photo FROM reports {ORDER_BY}"
        ).fetchall()
    return [{**dict(r), "has_photo": bool(r["has_photo"])} for r in rows]


def parse_new_report(data):
    if not isinstance(data, dict):
        raise ValueError("Formato inválido.")

    category = data.get("category")
    if category not in CATEGORIES:
        raise ValueError("Escolha um tipo de problema válido.")

    location_type = data.get("location_type")
    if location_type not in LOCATION_TYPES:
        raise ValueError("Escolha onde está o problema (rua, ciclovia ou calçada).")

    try:
        lat = float(data.get("lat"))
        lng = float(data.get("lng"))
    except (TypeError, ValueError):
        raise ValueError("Marque o local do problema no mapa.")
    # comparações com NaN/inf falham, então valores inválidos também são rejeitados aqui
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise ValueError("Localização inválida.")

    description = str(data.get("description") or "").strip()
    if len(description) > MAX_DESCRIPTION:
        raise ValueError(f"A descrição deve ter no máximo {MAX_DESCRIPTION} caracteres.")
    if category == "outro" and not description:
        raise ValueError("Para o tipo 'Outro', descreva o problema.")

    photo_bytes = None
    photo = data.get("photo")
    if photo:
        prefix = "data:image/jpeg;base64,"
        if not isinstance(photo, str) or not photo.startswith(prefix):
            raise ValueError("A foto deve estar em formato JPEG.")
        try:
            photo_bytes = base64.b64decode(photo[len(prefix):], validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("Foto inválida.")
        if len(photo_bytes) > MAX_PHOTO_BYTES:
            raise ValueError("A foto é grande demais.")
        if not photo_bytes.startswith(b"\xff\xd8\xff"):
            raise ValueError("A foto deve estar em formato JPEG.")

    return {
        "category": category,
        "location_type": location_type,
        "description": description,
        "lat": round(lat, 6),
        "lng": round(lng, 6),
        "photo": photo_bytes,
    }


def create_report(report):
    ts = now_iso()
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO reports (category, location_type, description, lat, lng, photo, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'aberto', ?, ?)",
            (report["category"], report["location_type"], report["description"],
             report["lat"], report["lng"], report["photo"], ts, ts),
        )
        return cur.lastrowid


def update_report(report_id, data):
    if not isinstance(data, dict):
        raise ValueError("Formato inválido.")
    fields = {}
    if "status" in data:
        if data["status"] not in STATUSES:
            raise ValueError("Status inválido.")
        fields["status"] = data["status"]
    if "staff_note" in data:
        note = str(data["staff_note"] or "").strip()
        if len(note) > MAX_NOTE:
            raise ValueError(f"A observação deve ter no máximo {MAX_NOTE} caracteres.")
        fields["staff_note"] = note
    if not fields:
        raise ValueError("Nada para atualizar.")
    fields["updated_at"] = now_iso()

    # As chaves vêm de uma lista fixa (status, staff_note, updated_at), nunca do usuário.
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with db() as conn:
        cur = conn.execute(
            f"UPDATE reports SET {assignments} WHERE id = ?", [*fields.values(), report_id]
        )
        return cur.rowcount > 0


def get_photo(report_id):
    with db() as conn:
        row = conn.execute("SELECT photo FROM reports WHERE id = ?", (report_id,)).fetchone()
    return row["photo"] if row else None


def csv_safe(value):
    # Evita "CSV injection" quando o arquivo for aberto no Excel.
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def export_csv():
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow([
        "id", "tipo", "local", "descricao", "latitude", "longitude", "status",
        "observacao_interna", "criado_em_utc", "atualizado_em_utc", "tem_foto", "link_mapa",
    ])
    for r in list_admin_reports():
        writer.writerow([
            r["id"],
            CATEGORIES.get(r["category"], r["category"]),
            LOCATION_TYPES.get(r["location_type"], r["location_type"]),
            csv_safe(r["description"]),
            r["lat"],
            r["lng"],
            STATUSES.get(r["status"], r["status"]),
            csv_safe(r["staff_note"]),
            r["created_at"],
            r["updated_at"],
            "sim" if r["has_photo"] else "não",
            f"https://www.openstreetmap.org/?mlat={r['lat']}&mlon={r['lng']}#map=19/{r['lat']}/{r['lng']}",
        ])
    return ("﻿" + out.getvalue()).encode("utf-8")  # BOM para o Excel reconhecer acentos


# --------------------------------------------------------------------------- servidor HTTP

class BodyTooLarge(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    server_version = "RuaSegura/1.0"
    sys_version = ""

    # ---- utilitários de resposta
    def _send(self, status, body, content_type, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        # O servidor de mapas do OpenStreetMap exige o cabeçalho Referer; mandamos só a origem.
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status, message):
        self._json({"error": message}, status)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            raise BodyTooLarge()
        if length <= 0:
            raise ValueError("Requisição vazia.")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("JSON inválido.")

    def _is_admin(self):
        pin = self.headers.get("X-Admin-Pin", "")
        return hmac.compare_digest(pin.encode("utf-8"), ADMIN_PIN.encode("utf-8"))

    def _serve_page(self, filename):
        body = (STATIC_DIR / filename).read_bytes()
        self._send(200, body, "text/html; charset=utf-8")

    # Logs sem endereço IP (dado pessoal desnecessário para o MVP).
    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # ---- rotas
    def do_GET(self):
        path = urlparse(self.path).path

        if path in STATIC_PAGES:
            return self._serve_page(STATIC_PAGES[path])

        if path == "/api/config":
            return self._json({
                "categories": CATEGORIES,
                "location_types": LOCATION_TYPES,
                "statuses": STATUSES,
                "map_center": MAP_CENTER,
                "map_zoom": MAP_ZOOM,
                "max_description": MAX_DESCRIPTION,
            })

        if path == "/api/reports":
            return self._json(list_public_reports())

        if path.startswith("/api/admin/"):
            if not self._is_admin():
                return self._error(401, "PIN inválido.")

            if path == "/api/admin/reports":
                return self._json(list_admin_reports())

            if path == "/api/admin/export.csv":
                filename = f"ocorrencias_{datetime.now().strftime('%Y-%m-%d')}.csv"
                return self._send(200, export_csv(), "text/csv; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{filename}"'})

            match = re.fullmatch(r"/api/admin/reports/(\d+)/photo", path)
            if match:
                photo = get_photo(int(match.group(1)))
                if not photo:
                    return self._error(404, "Foto não encontrada.")
                return self._send(200, photo, "image/jpeg")

        return self._error(404, "Não encontrado.")

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/reports":
            return self._error(404, "Não encontrado.")
        try:
            report = parse_new_report(self._read_json())
        except BodyTooLarge:
            self.close_connection = True
            return self._error(413, "Envio grande demais. Tente uma foto menor.")
        except ValueError as exc:
            return self._error(400, str(exc))
        report_id = create_report(report)
        return self._json({"id": report_id}, 201)

    def do_PATCH(self):
        path = urlparse(self.path).path
        match = re.fullmatch(r"/api/admin/reports/(\d+)", path)
        if not match:
            return self._error(404, "Não encontrado.")
        if not self._is_admin():
            return self._error(401, "PIN inválido.")
        try:
            found = update_report(int(match.group(1)), self._read_json())
        except BodyTooLarge:
            self.close_connection = True
            return self._error(413, "Requisição grande demais.")
        except ValueError as exc:
            return self._error(400, str(exc))
        if not found:
            return self._error(404, "Ocorrência não encontrada.")
        return self._json({"ok": True})


def main():
    global ADMIN_PIN
    init_db()
    if not ADMIN_PIN:
        ADMIN_PIN = secrets.token_urlsafe(6)
        print("Aviso: ADMIN_PIN não definido. PIN temporário desta execução: " + ADMIN_PIN)

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    shown_host = "localhost" if HOST in ("127.0.0.1", "0.0.0.0") else HOST
    print(f"Rua Segura rodando em http://{shown_host}:{PORT}")
    print(f"Painel da prefeitura: http://{shown_host}:{PORT}/admin")
    print("Pressione Ctrl+C para encerrar.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
