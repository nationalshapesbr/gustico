"""GusTico Barbearia — Sistema de fila simplificado."""

import sqlite3
import hashlib
from datetime import date, datetime
from functools import wraps
from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify, g, flash)

app = Flask(__name__)
app.secret_key = "gustico-2026"
SENHA_HASH = hashlib.sha256("admin123".encode()).hexdigest()

@app.template_filter("datebr")
def datebr_filter(val):
    """Converte YYYY-MM-DD para DD/MM/YYYY."""
    if not val or len(str(val)) < 10:
        return val or ""
    s = str(val)[:10]
    try:
        return f"{s[8:10]}/{s[5:7]}/{s[0:4]}"
    except Exception:
        return s

@app.template_filter("diames")
def diames_filter(val):
    """Converte YYYY-MM-DD para DD/MM."""
    if not val:
        return ""
    s = str(val)[:10]
    return f"{s[8:10]}/{s[5:7]}"

# ── Banco ────────────────────────────────────────────

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect("gustico.db")
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(_e=None):
    db = g.pop("db", None)
    if db:
        db.close()

def init_db():
    db = sqlite3.connect("gustico.db")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS servicos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            preco REAL NOT NULL DEFAULT 0,
            duracao INTEGER NOT NULL DEFAULT 30,
            ativo INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS fila (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            data TEXT NOT NULL,
            ordem INTEGER NOT NULL,
            status TEXT DEFAULT 'aguardando',
            chegada TEXT DEFAULT (datetime('now','localtime')),
            inicio TEXT, fim TEXT
        );
        CREATE TABLE IF NOT EXISTS fila_servicos (
            fila_id INTEGER REFERENCES fila(id) ON DELETE CASCADE,
            servico_id INTEGER REFERENCES servicos(id),
            PRIMARY KEY (fila_id, servico_id)
        );
        CREATE TABLE IF NOT EXISTS pagamentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fila_id INTEGER REFERENCES fila(id),
            valor REAL NOT NULL,
            metodo TEXT DEFAULT 'dinheiro',
            pago_em TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS config (
            chave TEXT PRIMARY KEY,
            valor TEXT NOT NULL
        );
    """)
    for c, v in [("fila_aberta", "1"), ("caixa_aberto", "0"), ("caixa_abertura", ""), ("caixa_fechamento", "")]:
        db.execute("INSERT OR IGNORE INTO config VALUES (?,?)", (c, v))
    for nome, preco, dur in [
        ("Corte Simples", 35, 30), ("Corte Degradê", 45, 40),
        ("Corte + Barba", 65, 60), ("Barba", 30, 25),
        ("Corte Infantil", 30, 25), ("Corte Feminino", 50, 45),
    ]:
        db.execute("INSERT INTO servicos(nome,preco,duracao) SELECT ?,?,? WHERE NOT EXISTS(SELECT 1 FROM servicos WHERE nome=?)",
                   (nome, preco, dur, nome))
    db.commit()
    db.close()

# ── Helpers ──────────────────────────────────────────

def cfg(chave, default="1"):
    r = get_db().execute("SELECT valor FROM config WHERE chave=?", (chave,)).fetchone()
    return r["valor"] if r else default

def set_cfg(chave, valor):
    get_db().execute("INSERT OR REPLACE INTO config VALUES(?,?)", (chave, valor))
    get_db().commit()

def hoje():
    return date.today().isoformat()

def agora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return f(*a, **k)
    return w

def pegar_fila(data, so_ativos=False):
    filtro = "AND f.status IN ('aguardando','atendendo')" if so_ativos else "AND f.status != 'cancelado'"
    return get_db().execute(f"""
        SELECT f.*, GROUP_CONCAT(s.nome,' + ') AS servicos,
               COALESCE(SUM(s.preco),0) AS total,
               COALESCE(SUM(s.duracao),0) AS duracao,
               p.id AS pago_id, p.valor AS pago_valor, p.metodo
        FROM fila f
        LEFT JOIN fila_servicos fs ON fs.fila_id=f.id
        LEFT JOIN servicos s ON s.id=fs.servico_id
        LEFT JOIN pagamentos p ON p.fila_id=f.id
        WHERE f.data=? {filtro}
        GROUP BY f.id
        ORDER BY CASE f.status WHEN 'atendendo' THEN 0 WHEN 'aguardando' THEN 1 ELSE 2 END,
                 CASE WHEN f.status='concluido' THEN f.fim END ASC, f.ordem
    """, (data,)).fetchall()

def calc_espera(fila):
    espera, acum = {}, 0
    for r in fila:
        dur = r["duracao"] or 30
        if r["status"] == "concluido":
            espera[r["id"]] = 0
        elif r["status"] == "atendendo":
            espera[r["id"]] = 0
            if r["inicio"]:
                try:
                    dec = int((datetime.now() - datetime.strptime(r["inicio"], "%Y-%m-%d %H:%M:%S")).total_seconds()) // 60
                    acum += max(0, dur - dec)
                except ValueError:
                    acum += dur
            else:
                acum += dur
        else:
            espera[r["id"]] = acum
            acum += dur
    return espera

# ── Páginas ──────────────────────────────────────────

@app.route("/")
def index():
    db = get_db()
    servicos = db.execute("SELECT * FROM servicos WHERE ativo=1 ORDER BY nome").fetchall()
    fila = pegar_fila(hoje(), so_ativos=True)
    return render_template("index.html", servicos=servicos, fila=fila,
                           espera=calc_espera(fila), fila_aberta=cfg("fila_aberta")=="1")

@app.route("/api/fila-publica")
def api_fila_publica():
    """JSON da fila ao vivo para polling em tempo real."""
    fila = pegar_fila(hoje(), so_ativos=True)
    espera = calc_espera(fila)
    items = []
    for i, f in enumerate(fila, 1):
        items.append({
            "pos": i, "nome": f["nome"], "servicos": f["servicos"] or "—",
            "status": f["status"], "espera_min": espera.get(f["id"], 0),
            "inicio": f["inicio"] or "", "duracao": f["duracao"] or 30
        })
    return jsonify(fila=items, fila_aberta=cfg("fila_aberta")=="1",
                   total=len(items), hora=datetime.now().strftime("%H:%M:%S"))

@app.route("/entrar", methods=["POST"])
def entrar():
    db = get_db()
    nome = request.form.get("nome","").strip()
    ids = request.form.getlist("servico_ids")
    if cfg("fila_aberta") != "1":
        flash("Fila fechada!", "erro")
        return redirect(url_for("index"))
    if not nome or not ids:
        flash("Preencha nome e escolha um serviço.", "erro")
        return redirect(url_for("index"))
    r = db.execute("SELECT COALESCE(MAX(ordem),0)+1 AS p FROM fila WHERE data=? AND status!='cancelado'", (hoje(),)).fetchone()
    cur = db.execute("INSERT INTO fila(nome,data,ordem) VALUES(?,?,?)", (nome, hoje(), r["p"]))
    for sid in ids:
        db.execute("INSERT OR IGNORE INTO fila_servicos VALUES(?,?)", (cur.lastrowid, int(sid)))
    db.commit()
    flash(f"✅ {nome}, você é o #{r['p']} da fila!", "ok")
    return redirect(url_for("index"))

@app.route("/admin/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        if hashlib.sha256(request.form.get("senha","").encode()).hexdigest() == SENHA_HASH:
            session["admin"] = True
            return redirect(url_for("admin_fila"))
        flash("Senha incorreta.", "erro")
    return render_template("login.html")

@app.route("/admin/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/admin")
@app.route("/admin/fila")
@admin_required
def admin_fila():
    data = request.args.get("data", hoje())
    fila = pegar_fila(data)
    total = get_db().execute("SELECT COALESCE(SUM(p.valor),0) AS t FROM pagamentos p JOIN fila f ON f.id=p.fila_id WHERE f.data=?", (data,)).fetchone()["t"]
    return render_template("admin_fila.html", fila=fila, espera=calc_espera(fila),
                           data=data, hoje=hoje(), total_dia=total,
                           fila_aberta=cfg("fila_aberta")=="1", pagina="fila")

@app.route("/admin/financeiro")
@admin_required
def admin_financeiro():
    db = get_db()
    mes = date.today().strftime("%Y-%m")
    por_dia = db.execute("""
        SELECT f.data AS dia, COALESCE(SUM(p.valor),0) AS total, COUNT(DISTINCT f.id) AS qtd
        FROM fila f LEFT JOIN pagamentos p ON p.fila_id=f.id
        WHERE f.status='concluido' AND f.data LIKE ?
        GROUP BY f.data ORDER BY f.data
    """, (f"{mes}%",)).fetchall()
    total_mes = sum(r["total"] for r in por_dia)
    total_geral = db.execute("SELECT COALESCE(SUM(valor),0) AS t FROM pagamentos").fetchone()["t"]
    total_hoje = db.execute("""
        SELECT COALESCE(SUM(p.valor),0) AS t
        FROM pagamentos p JOIN fila f ON f.id=p.fila_id WHERE f.data=?
    """, (hoje(),)).fetchone()["t"]
    atend_hoje = db.execute("SELECT COUNT(*) AS c FROM fila WHERE data=? AND status='concluido'", (hoje(),)).fetchone()["c"]
    dias_grafico = [{"dia": r["dia"], "total": r["total"], "qtd": r["qtd"]} for r in por_dia]
    return render_template("admin_financeiro.html", por_dia=por_dia,
                           total_mes=total_mes, total_geral=total_geral,
                           total_hoje=total_hoje, atend_hoje=atend_hoje,
                           dias_grafico=dias_grafico, hoje=hoje(),
                           caixa_aberto=cfg("caixa_aberto")=="1",
                           caixa_abertura=cfg("caixa_abertura",""),
                           caixa_fechamento=cfg("caixa_fechamento",""),
                           pagina="financeiro")

@app.route("/admin/servicos")
@admin_required
def admin_servicos():
    servicos = get_db().execute("SELECT * FROM servicos ORDER BY nome").fetchall()
    return render_template("admin_servicos.html", servicos=servicos, pagina="servicos")

# ── APIs ─────────────────────────────────────────────

@app.route("/api/status/<int:fid>", methods=["POST"])
@admin_required
def api_status(fid):
    st = request.json.get("status","")
    if st not in ("aguardando","atendendo","concluido","cancelado"):
        return jsonify(erro="inválido"), 400
    updates = {"status": st}
    if st == "atendendo": updates["inicio"] = agora()
    if st == "concluido": updates["fim"] = agora()
    sets = ", ".join(f"{k}=?" for k in updates)
    get_db().execute(f"UPDATE fila SET {sets} WHERE id=?", [*updates.values(), fid])
    get_db().commit()
    return jsonify(ok=True)

@app.route("/api/pagar/<int:fid>", methods=["POST"])
@admin_required
def api_pagar(fid):
    d = request.json
    db = get_db()
    db.execute("DELETE FROM pagamentos WHERE fila_id=?", (fid,))
    db.execute("INSERT INTO pagamentos(fila_id,valor,metodo) VALUES(?,?,?)",
               (fid, float(d["valor"]), d.get("metodo","dinheiro")))
    db.commit()
    return jsonify(ok=True)

@app.route("/api/cancelar/<int:fid>", methods=["POST"])
@admin_required
def api_cancelar(fid):
    get_db().execute("UPDATE fila SET status='cancelado' WHERE id=?", (fid,))
    get_db().commit()
    return jsonify(ok=True)

@app.route("/api/servico", methods=["POST"])
@admin_required
def api_novo_servico():
    d = request.json
    get_db().execute("INSERT INTO servicos(nome,preco,duracao) VALUES(?,?,?)",
                     (d["nome"], float(d["preco"]), int(d.get("duracao",30))))
    get_db().commit()
    return jsonify(ok=True)

@app.route("/api/servico/<int:sid>", methods=["PUT"])
@admin_required
def api_editar_servico(sid):
    d = request.json
    get_db().execute("UPDATE servicos SET nome=?,preco=?,duracao=?,ativo=? WHERE id=?",
                     (d["nome"], float(d["preco"]), int(d.get("duracao",30)), int(d.get("ativo",1)), sid))
    get_db().commit()
    return jsonify(ok=True)

@app.route("/api/servico/<int:sid>/toggle", methods=["POST"])
@admin_required
def api_toggle_servico(sid):
    d = request.json
    get_db().execute("UPDATE servicos SET ativo=? WHERE id=?", (int(d.get("ativo",1)), sid))
    get_db().commit()
    return jsonify(ok=True)

@app.route("/api/caixa/toggle", methods=["POST"])
@admin_required
def api_toggle_caixa():
    abrir = request.json.get("abrir", False)
    set_cfg("caixa_aberto", "1" if abrir else "0")
    if abrir:
        set_cfg("caixa_abertura", agora())
        set_cfg("caixa_fechamento", "")
    else:
        set_cfg("caixa_fechamento", agora())
    return jsonify(ok=True)

@app.route("/api/fila/toggle", methods=["POST"])
@admin_required
def api_toggle_fila():
    abrir = request.json.get("abrir", False)
    set_cfg("fila_aberta", "1" if abrir else "0")
    return jsonify(ok=True)

@app.route("/api/reordenar", methods=["POST"])
@admin_required
def api_reordenar():
    for i, fid in enumerate(request.json.get("ids",[]), 1):
        get_db().execute("UPDATE fila SET ordem=? WHERE id=?", (i, fid))
    get_db().commit()
    return jsonify(ok=True)

# ── Main ─────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    print("\n💇 GusTico rodando em http://localhost:5000\n")
    app.run(debug=True, port=5000)
