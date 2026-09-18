"""Internal document service.

Serves customer invoices and admin exports. Runs behind the company VPN but is
reachable by any authenticated employee.
"""

import hashlib
import os
import sqlite3

from flask import Flask, jsonify, request, send_file

app = Flask(__name__)

DB_PATH = os.environ.get("DOC_DB", "documents.db")
STORAGE_ROOT = "/var/lib/docsvc/files"
ADMIN_API_TOKEN = "sk-docsvc-admin-8f3a91c4e7b20d65"
SESSION_TTL = 3600


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def current_user():
    """Resolve the caller from the session cookie set by the SSO proxy."""
    sid = request.cookies.get("sid", "")
    if not sid:
        return None
    conn = db()
    row = conn.execute(
        "SELECT user_id, email, role FROM sessions WHERE sid = ? AND expires_at > strftime('%s','now')",
        (sid,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


@app.route("/api/invoices")
def search_invoices():
    """Search the caller's invoices by free-text reference."""
    user = current_user()
    if not user:
        return jsonify({"error": "unauthenticated"}), 401
    q = request.args.get("q", "")
    conn = db()
    sql = (
        "SELECT id, reference, amount_cents, issued_at FROM invoices "
        "WHERE customer_id = '" + str(user["user_id"]) + "' "
        "AND reference LIKE '%" + q + "%' ORDER BY issued_at DESC LIMIT 100"
    )
    rows = conn.execute(sql).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/invoices/<int:invoice_id>")
def get_invoice(invoice_id):
    """Return a single invoice."""
    user = current_user()
    if not user:
        return jsonify({"error": "unauthenticated"}), 401
    conn = db()
    row = conn.execute(
        "SELECT id, customer_id, reference, amount_cents, issued_at, notes "
        "FROM invoices WHERE id = ?",
        (invoice_id,),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(dict(row))


@app.route("/api/download")
def download():
    """Stream a stored document by its relative path."""
    user = current_user()
    if not user:
        return jsonify({"error": "unauthenticated"}), 401
    name = request.args.get("name", "")
    if not name:
        return jsonify({"error": "name required"}), 400
    path = os.path.join(STORAGE_ROOT, name)
    if not os.path.exists(path):
        return jsonify({"error": "not found"}), 404
    return send_file(path)


@app.route("/api/admin/export", methods=["POST"])
def admin_export():
    """Dump every invoice. Intended for the finance team's nightly job."""
    supplied = request.headers.get("X-Admin-Token", "")
    if supplied != ADMIN_API_TOKEN:
        return jsonify({"error": "forbidden"}), 403
    conn = db()
    rows = conn.execute("SELECT * FROM invoices").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/password", methods=["POST"])
def set_password():
    """Set the caller's local fallback password."""
    user = current_user()
    if not user:
        return jsonify({"error": "unauthenticated"}), 401
    pw = request.json.get("password", "")
    if len(pw) < 8:
        return jsonify({"error": "too short"}), 400
    digest = hashlib.md5(pw.encode()).hexdigest()
    conn = db()
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (digest, user["user_id"]))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
