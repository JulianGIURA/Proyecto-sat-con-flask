from __future__ import annotations
import os, io, secrets, urllib.parse
from datetime import datetime
from typing import Optional
from werkzeug.utils import secure_filename

from flask import Flask, render_template, request, redirect, url_for, flash, send_file
from flask_sqlalchemy import SQLAlchemy


from flask_login import (
    LoginManager,
    login_user,
    logout_user,
    login_required,
    current_user,
    UserMixin,
)
from werkzeug.security import generate_password_hash, check_password_hash
from reportlab.lib.pagesizes import A5, landscape
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
import qrcode

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "sat.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "dev-secret")

# ========================================
#   BASE DE DATOS: POSTGRES EN RENDER
#   y SQLITE en local (fallback)
# ========================================

database_url = os.getenv("DATABASE_URL")  # Render

if database_url:
    # Fix necesario porque SQLAlchemy requiere 'postgresql://'
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)

    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
else:
    # Local → SQLite
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"

# No cambiar
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024

db = SQLAlchemy(app)


login_manager = LoginManager(app)
login_manager.login_view = "login_form"  # Vista donde se redirige al intentar entrar sin login


ORDER_STATES = [
    ("recibido", "Recibido"),
    ("diagnostico", "Diagnóstico"),
    ("en_proceso", "En proceso"),
    ("esperando_repuestos", "Esperando repuestos"),
    ("listo", "Listo para entregar"),
    ("entregado", "Entregado"),
    ("cancelado", "Cancelado"),
]

def gen_token(n=10):
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))

class Settings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    empresa = db.Column(db.String(120))
    direccion = db.Column(db.String(200))
    telefono = db.Column(db.String(50))
    email = db.Column(db.String(120))
    logo_filename = db.Column(db.String(200))
    condiciones = db.Column(db.Text)  # ← NUEVO CAMPO PARA GARANTÍAS
  

class Client(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), nullable=False)
    telefono = db.Column(db.String(50))
    email = db.Column(db.String(120))
    direccion = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    dni = db.Column(db.String(20), nullable=True)


class RepairOrder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("client.id"), nullable=False)
    client = db.relationship("Client", backref=db.backref("orders", lazy=True))

    marca = db.Column(db.String(80), nullable=False)
    modelo = db.Column(db.String(120), nullable=False)
    imei = db.Column(db.String(40))
    accesorios = db.Column(db.String(200))
    clave_desbloqueo = db.Column(db.String(120))

    problema_reportado = db.Column(db.Text, nullable=False)
    diagnostico = db.Column(db.Text)
    costo_estimado = db.Column(db.Float)
    senia = db.Column(db.Float, default=0.0)

    estado = db.Column(db.String(40), default="recibido", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    token_publico = db.Column(db.String(16), unique=True, default=lambda: gen_token(10))

    def estado_label(self):
        return dict(ORDER_STATES).get(self.estado, self.estado)

class StatusHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("repair_order.id"), nullable=False)
    order = db.relationship("RepairOrder", backref=db.backref("historial", lazy=True, order_by="StatusHistory.created_at.desc()"))
    estado = db.Column(db.String(40), nullable=False)
    nota = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Part(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("repair_order.id"), nullable=False)
    order = db.relationship("RepairOrder", backref=db.backref("repuestos", lazy=True))
    descripcion = db.Column(db.String(200), nullable=False)
    costo = db.Column(db.Float, nullable=False, default=0.0)

class CashEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    fecha = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    tipo = db.Column(db.String(10), nullable=False)  # entrada/salida
    concepto = db.Column(db.String(200), nullable=False)
    monto = db.Column(db.Float, nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey("repair_order.id"))
    order = db.relationship("RepairOrder", backref=db.backref("movimientos_caja", lazy=True))

# ============================
#   USUARIOS PARA LOGIN
# ============================
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default="tecnico")  # admin / tecnico / cajero

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def parse_float(val: Optional[str]) -> Optional[float]:
    if val is None or val.strip() == "":
        return None
    try:
        return float(val.replace(",", "."))
    except ValueError:
        return None

def get_settings() -> Settings:
    s = Settings.query.get(1)
    if not s:
        s = Settings(id=1)
        db.session.add(s); db.session.commit()
    return s

@app.cli.command("seed")
def seed():
    db.create_all()
    s = get_settings()
    if not Client.query.first():
        c1 = Client(nombre="Juan Pérez", telefono="261-555-1234", email="juan@example.com", direccion="San Martín 123")
        db.session.add(c1); db.session.commit()
        o1 = RepairOrder(client=c1, marca="Samsung", modelo="A54", imei="3598...", accesorios="Funda", clave_desbloqueo="1-2-5-8",
                         problema_reportado="No carga", diagnostico="Conector", costo_estimado=45000, senia=10000, estado="diagnostico")
        db.session.add(o1); db.session.commit()
        db.session.add(StatusHistory(order=o1, estado="recibido", nota="Ingreso"))
        db.session.add(StatusHistory(order=o1, estado="diagnostico", nota="Se detecta conector"))
        db.session.add(Part(order=o1, descripcion="Conector USB-C", costo=12000.0))
        db.session.add(CashEntry(tipo="entrada", concepto=f"Seña orden #{o1.id}", monto=10000.0, order=o1))
        db.session.commit()
        print("DB demo creada.")

@app.context_processor
def inject_settings():
    # Obtenemos la configuración principal
    settings = get_settings()

    # La exponemos con las dos claves:
    # - 'settings'  (por si algún template viejo la usa)
    # - 'app_settings' (para base.html y otros nuevos)
    return {
        "settings": settings,
        "app_settings": settings,
    }

@app.before_request
def ensure_db():
    if not os.path.exists(DB_PATH):
        db.create_all()



@app.get("/")
@login_required
def index():
    total_orders = RepairOrder.query.count()
    open_orders = RepairOrder.query.filter(RepairOrder.estado.in_(["recibido","diagnostico","en_proceso","esperando_repuestos"])).count()
    ready_orders = RepairOrder.query.filter_by(estado="listo").count()
    delivered_orders = RepairOrder.query.filter_by(estado="entregado").count()
    recent = RepairOrder.query.order_by(RepairOrder.created_at.desc()).limit(8).all()
    entradas = db.session.query(db.func.coalesce(db.func.sum(CashEntry.monto),0.0)).filter(CashEntry.tipo=="entrada").scalar() or 0.0
    salidas = db.session.query(db.func.coalesce(db.func.sum(CashEntry.monto),0.0)).filter(CashEntry.tipo=="salida").scalar() or 0.0
    saldo = entradas - salidas
    return render_template("index.html", total_orders=total_orders, open_orders=open_orders,
                           ready_orders=ready_orders, delivered_orders=delivered_orders, recent=recent, saldo=saldo)

# Settings
@app.get("/settings")
@login_required
def settings_view():
    return render_template("settings.html", s=get_settings())

@app.post("/settings")
@login_required
def settings_save():
    s = get_settings()
    s.empresa = request.form.get("empresa","").strip()
    s.telefono = request.form.get("telefono","").strip()
    s.email = request.form.get("email","").strip()
    s.direccion = request.form.get("direccion","").strip()
    s.condiciones = request.form.get("condiciones", "").strip()
    file = request.files.get("logo")
    if file and file.filename:
        fn = file.filename.lower()
        if not (fn.endswith(".png") or fn.endswith(".jpg") or fn.endswith(".jpeg")):
            flash("El logo debe ser PNG/JPG.", "danger")
            return redirect(url_for("settings_view"))
        safe = f"logo_{secrets.token_hex(4)}" + (".png" if fn.endswith(".png") else ".jpg")
        file.save(os.path.join(UPLOAD_DIR, safe))
        s.logo_filename = safe
    db.session.commit()
    flash("Configuración guardada", "success")
    return redirect(url_for("settings_view"))

# ============================
# LOGIN / LOGOUT
# ============================

@app.get("/login")
def login_form():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    return render_template("login.html")

@app.post("/login")
def login():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()

    user = User.query.filter_by(username=username).first()
    if not user or not user.check_password(password):
        flash("Usuario o contraseña inválidos.", "danger")
        return redirect(url_for("login_form"))

    login_user(user)
    flash("Sesión iniciada.", "success")
    next_url = request.args.get("next") or url_for("index")
    return redirect(next_url)

@app.get("/logout")
@login_required
def logout():
    logout_user()
    flash("Sesión cerrada.", "success")
    return redirect(url_for("login_form"))


# Clients
from sqlalchemy import or_
@app.get("/clients")
@login_required

def clients_list():
    q = request.args.get("q", "").strip()
    query = Client.query
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Client.nombre.ilike(like), Client.telefono.ilike(like), Client.email.ilike(like), Client.direccion.ilike(like), Client.dni.ilike(like)))
    clients = query.order_by(Client.created_at.desc()).all()
    return render_template("clients_list.html", clients=clients, q=q)

@app.get("/clients/new")
@login_required

def client_new_form():
    return render_template("client_form.html", client=None)

@app.post("/clients/new")
@login_required

def client_create():
    nombre = request.form.get("nombre", "").strip()
    telefono = request.form.get("telefono", "").strip()
    email = request.form.get("email", "").strip()
    direccion = request.form.get("direccion", "").strip()
    dni = request.form.get("dni", "").strip()

    if not nombre:
        flash("El nombre es obligatorio", "danger")
        return redirect(url_for("client_new_form"))

    if not dni:
        flash("El DNI es obligatorio", "danger")
        return redirect(url_for("client_new_form"))

    c = Client(
        nombre=nombre,
        telefono=telefono,
        email=email,
        direccion=direccion,
        dni=dni,
    )
    db.session.add(c)
    db.session.commit()
    flash("Cliente creado", "success")
    return redirect(url_for("clients_list"))

@app.get("/clients/<int:client_id>/edit")
@login_required

def client_edit_form(client_id: int):
    client = Client.query.get_or_404(client_id)
    return render_template("client_form.html", client=client)

@app.post("/clients/<int:client_id>/edit")
@login_required

def client_update(client_id: int):
    client = Client.query.get_or_404(client_id)

    nombre = request.form.get("nombre", "").strip()
    telefono = request.form.get("telefono", "").strip()
    email = request.form.get("email", "").strip()
    direccion = request.form.get("direccion", "").strip()
    dni = request.form.get("dni", "").strip()

    if not nombre:
        flash("El nombre es obligatorio", "danger")
        return redirect(url_for("client_edit_form", client_id=client.id))

    if not dni:
        flash("El DNI es obligatorio", "danger")
        return redirect(url_for("client_edit_form", client_id=client.id))

    client.nombre = nombre
    client.telefono = telefono
    client.email = email
    client.direccion = direccion
    client.dni = dni

    db.session.commit()
    flash("Cliente actualizado", "success")
    return redirect(url_for("clients_list"))

# Orders
@app.get("/orders")
@login_required

def orders_list():
    q = request.args.get("q","").strip()
    estado = request.args.get("estado","").strip()
    query = RepairOrder.query.join(Client)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(RepairOrder.id.like(f"%{q}%"), RepairOrder.imei.ilike(like),
                                 RepairOrder.marca.ilike(like), RepairOrder.modelo.ilike(like),
                                 Client.nombre.ilike(like), Client.dni.ilike(like) ))
    if estado:
        query = query.filter(RepairOrder.estado==estado)
    orders = query.order_by(RepairOrder.created_at.desc()).all()
    return render_template("orders_list.html", orders=orders, q=q, estado=estado, estados=ORDER_STATES)

@app.get("/orders/new")
@login_required

def order_new_form():
    clients = Client.query.order_by(Client.nombre.asc()).all()
    return render_template("order_form.html", order=None, clients=clients, estados=ORDER_STATES)

@app.post("/orders/new")
@login_required

def order_create():
    client_id = request.form.get("client_id")
    client = Client.query.get(client_id)
    if not client:
        flash("Debe seleccionar un cliente válido.", "danger")
        return redirect(url_for("order_new_form"))
    marca = request.form.get("marca","").strip()
    modelo = request.form.get("modelo","").strip()
    problema = request.form.get("problema_reportado","").strip()
    if not marca or not modelo or not problema:
        flash("Marca, modelo y problema reportado son obligatorios.", "danger")
        return redirect(url_for("order_new_form"))
    order = RepairOrder(
        client=client,
        marca=marca,
        modelo=modelo,
        imei=request.form.get("imei","").strip(),
        accesorios=request.form.get("accesorios","").strip(),
        clave_desbloqueo=request.form.get("clave_desbloqueo","").strip(),
        problema_reportado=problema,
        diagnostico=request.form.get("diagnostico","").strip(),
        costo_estimado=parse_float(request.form.get("costo_estimado")),
        senia=parse_float(request.form.get("senia")) or 0.0,
        estado=request.form.get("estado","recibido"),
    )
    db.session.add(order); db.session.commit()
    db.session.add(StatusHistory(order=order, estado=order.estado, nota="Ingreso de la orden"))
    db.session.commit()
    if order.senia and order.senia>0:
        db.session.add(CashEntry(tipo="entrada", concepto=f"Seña orden #{order.id}", monto=order.senia, order=order))
        db.session.commit()
    flash(f"Orden #{order.id} creada.", "success")
    return redirect(url_for("orders_list"))

@app.get("/orders/<int:order_id>")
@login_required

def order_detail(order_id: int):
    order = RepairOrder.query.get_or_404(order_id)
    public_url = url_for("order_public", token=order.token_publico, _external=True)
    text = f"Hola {order.client.nombre}, te compartimos el estado de tu orden #{order.id}: {public_url}"
    wa_link = "https://wa.me/?text=" + urllib.parse.quote(text)
    total_repuestos = sum([(p.costo or 0.0) for p in order.repuestos])
    return render_template("order_detail.html", order=order, estados=ORDER_STATES, public_url=public_url, wa_link=wa_link, total_repuestos=total_repuestos)

@app.get("/orders/<int:order_id>/edit")
@login_required

def order_edit_form(order_id: int):
    order = RepairOrder.query.get_or_404(order_id)
    clients = Client.query.order_by(Client.nombre.asc()).all()
    return render_template("order_form.html", order=order, clients=clients, estados=ORDER_STATES)

@app.post("/orders/<int:order_id>/edit")
@login_required

def order_update(order_id: int):
    order = RepairOrder.query.get_or_404(order_id)
    client_id = request.form.get("client_id")
    client = Client.query.get(client_id)
    if not client:
        flash("Cliente inválido.", "danger")
        return redirect(url_for("order_edit_form", order_id=order.id))
    order.client = client
    order.marca = request.form.get("marca","").strip()
    order.modelo = request.form.get("modelo","").strip()
    order.imei = request.form.get("imei","").strip()
    order.accesorios = request.form.get("accesorios","").strip()
    order.clave_desbloqueo = request.form.get("clave_desbloqueo","").strip()
    order.problema_reportado = request.form.get("problema_reportado","").strip()
    order.diagnostico = request.form.get("diagnostico","").strip()
    order.costo_estimado = parse_float(request.form.get("costo_estimado"))
    order.senia = parse_float(request.form.get("senia")) or 0.0
    db.session.commit()
    flash("Orden actualizada.", "success")
    return redirect(url_for("order_detail", order_id=order.id))

@app.post("/orders/<int:order_id>/status")
@login_required

def order_change_status(order_id: int):
    order = RepairOrder.query.get_or_404(order_id)
    nuevo_estado = request.form.get("estado")
    nota = request.form.get("nota", "").strip()

    if nuevo_estado not in dict(ORDER_STATES):
        flash("Estado inválido.", "danger")
        return redirect(url_for("order_detail", order_id=order.id))

    # Actualiza estado y guarda historial
    order.estado = nuevo_estado
    db.session.add(StatusHistory(order=order, estado=nuevo_estado, nota=nota))

    # =========================
    #  LÓGICA DE CAJA
    # =========================

    # 1) Si se entrega el equipo, registramos el cobro restante en caja
    if nuevo_estado == "entregado" and order.costo_estimado and order.costo_estimado > 0:
        # Total ya cobrado (por seña u otros pagos ligados a la orden)
        total_pagado = sum(
            (mov.monto or 0.0)
            for mov in order.movimientos_caja
            if mov.tipo == "entrada"
        )

        restante = round(order.costo_estimado - total_pagado, 2)

        # Solo genera movimiento si realmente falta cobrar algo
        if restante > 0:
            concepto = f"Pago final orden #{order.id}"
            db.session.add(
                CashEntry(
                    tipo="entrada",
                    concepto=concepto,
                    monto=restante,
                    order=order,
                )
            )

    # 2) Si se cancela la orden, devolvemos la seña en caja (salida)
    elif nuevo_estado == "cancelado" and order.senia and order.senia > 0:
        # Total ya devuelto previamente (por si se cambió varias veces de estado)
        total_devuelto = sum(
            (mov.monto or 0.0)
            for mov in order.movimientos_caja
            if mov.tipo == "salida" and (mov.concepto or "").startswith("Devolución seña")
        )

        a_devolver = round(order.senia - total_devuelto, 2)

        # Solo generamos movimiento si todavía queda algo por devolver
        if a_devolver > 0:
            concepto = f"Devolución seña orden #{order.id}"
            db.session.add(
                CashEntry(
                    tipo="salida",
                    concepto=concepto,
                    monto=a_devolver,
                    order=order,
                )
            )

    db.session.commit()
    flash("Estado actualizado.", "success")
    return redirect(url_for("order_detail", order_id=order.id))

# Parts internal
@app.post("/orders/<int:order_id>/parts/add")
@login_required

def add_part(order_id: int):
    order = RepairOrder.query.get_or_404(order_id)
    desc = request.form.get("descripcion","").strip()
    costo = parse_float(request.form.get("costo")) or 0.0
    if not desc:
        flash("Descripción de repuesto requerida.", "danger")
        return redirect(url_for("order_detail", order_id=order.id))
    db.session.add(Part(order=order, descripcion=desc, costo=costo)); db.session.commit()
    flash("Repuesto agregado.", "success")
    return redirect(url_for("order_detail", order_id=order.id))

@app.post("/orders/<int:order_id>/parts/<int:part_id>/del")
@login_required

def del_part(order_id: int, part_id: int):
    part = Part.query.get_or_404(part_id)
    db.session.delete(part); db.session.commit()
    flash("Repuesto eliminado.", "success")
    return redirect(url_for("order_detail", order_id=order_id))

# Cash
@app.get("/cash")
@login_required

def cash_list():
    entradas = db.session.query(db.func.coalesce(db.func.sum(CashEntry.monto),0.0)).filter(CashEntry.tipo=="entrada").scalar() or 0.0
    salidas = db.session.query(db.func.coalesce(db.func.sum(CashEntry.monto),0.0)).filter(CashEntry.tipo=="salida").scalar() or 0.0
    saldo = entradas - salidas
    rows = CashEntry.query.order_by(CashEntry.fecha.desc()).limit(200).all()
    return render_template("cash_list.html", rows=rows, entradas=entradas, salidas=salidas, saldo=saldo)

@app.get("/cash/new")
@login_required

def cash_new_form():
    orders = RepairOrder.query.order_by(RepairOrder.created_at.desc()).limit(50).all()
    return render_template("cash_form.html", orders=orders)

@app.post("/cash/new")
@login_required

def cash_create():
    tipo = request.form.get("tipo")
    concepto = request.form.get("concepto","").strip()
    monto = parse_float(request.form.get("monto")) or 0.0
    order_id = request.form.get("order_id")
    if tipo not in ("entrada","salida") or not concepto or monto<=0:
        flash("Completar tipo, concepto y monto (>0).", "danger")
        return redirect(url_for("cash_new_form"))
    entry = CashEntry(tipo=tipo, concepto=concepto, monto=monto, order=RepairOrder.query.get(order_id) if order_id else None)
    db.session.add(entry); db.session.commit()
    flash("Movimiento registrado.", "success")
    return redirect(url_for("cash_list"))

# PDF
def _qr_bytes_for_url(url: str) -> bytes:
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

@app.get("/orders/<int:order_id>/pdf")
@login_required

def order_pdf(order_id: int):
    o = RepairOrder.query.get_or_404(order_id)
    s = get_settings()
    public_url = url_for("order_public", token=o.token_publico, _external=True)
    qr_png = _qr_bytes_for_url(public_url)

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=landscape(A5))
    width, height = landscape(A5)

    # Logo
    if s.logo_filename:
        from reportlab.lib.utils import ImageReader
        try:
            logo_path = os.path.join(UPLOAD_DIR, s.logo_filename)
            c.drawImage(
                ImageReader(logo_path),
                10 * mm,
                height - 25 * mm,
                30 * mm,
                20 * mm,
                preserveAspectRatio=True,
                mask="auto",
            )
        except Exception:
            pass

    # Cabecera empresa
    c.setFont("Helvetica-Bold", 14)
    c.drawString(45 * mm, height - 15 * mm, s.empresa or "Servicio Técnico")
    c.setFont("Helvetica", 9)
    sub = " · ".join([x for x in [s.direccion, s.telefono, s.email] if x])
    if sub:
        c.drawString(45 * mm, height - 21 * mm, sub)

    # Datos principales de la orden
    c.setFont("Helvetica-Bold", 12)
    c.drawString(10 * mm, height - 32 * mm, f"Orden #{o.id} — {o.estado_label()}")
    c.setFont("Helvetica", 10)
    c.drawString(10 * mm, height - 38 * mm, f"Ingreso: {o.created_at.strftime('%d/%m/%Y %H:%M')}")

    # Cliente
    c.setFont("Helvetica-Bold", 11)
    c.drawString(10 * mm, height - 48 * mm, "Cliente")
    c.setFont("Helvetica", 10)
    c.drawString(
        10 * mm,
        height - 54 * mm,
        f"{o.client.nombre}  {o.client.telefono or ''}  {o.client.email or ''}",
    )

    # Equipo
    c.setFont("Helvetica-Bold", 11)
    c.drawString(10 * mm, height - 64 * mm, "Equipo")
    c.setFont("Helvetica", 10)
    c.drawString(
        10 * mm,
        height - 70 * mm,
        f"{o.marca} {o.modelo} · IMEI: {o.imei or '—'} · Accesorios: {o.accesorios or '—'}",
    )

    # Problema reportado
    c.setFont("Helvetica-Bold", 11)
    c.drawString(10 * mm, height - 82 * mm, "Problema reportado")
    t1 = c.beginText(10 * mm, height - 88 * mm)
    t1.setFont("Helvetica", 10)
    t1.textLines(o.problema_reportado or "")
    c.drawText(t1)

    # Diagnóstico
    c.setFont("Helvetica-Bold", 11)
    c.drawString(10 * mm, height - 102 * mm, "Diagnóstico")
    t2 = c.beginText(10 * mm, height - 108 * mm)
    t2.setFont("Helvetica", 10)
    t2.textLines(o.diagnostico or "—")
    c.drawText(t2)

    # Costos
    c.setFont("Helvetica-Bold", 11)
    c.drawString(10 * mm, height - 120 * mm, "Costos")
    c.setFont("Helvetica", 10)
    costo = (
        f"AR$ {o.costo_estimado:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        if o.costo_estimado is not None
        else "—"
    )
    senia = (
        f"AR$ {o.senia:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        if o.senia
        else "—"
    )
    c.drawString(
        10 * mm,
        height - 126 * mm,
        f"Costo estimado: {costo} · Seña: {senia}",
    )

    # QR + link público
    from reportlab.lib.utils import ImageReader
    c.drawString(width - 60 * mm, height - 84 * mm, "Seguimiento online:")
    c.setFont("Helvetica", 8)
    c.drawString(
        width - 60 * mm,
        height - 88 * mm,
        public_url[:60] + ("..." if len(public_url) > 60 else ""),
    )
    c.drawImage(
        ImageReader(io.BytesIO(qr_png)),
        width - 60 * mm,
        height - 80 * mm,
        40 * mm,
        40 * mm,
        preserveAspectRatio=True,
        mask="auto",
    )

    # Condiciones / garantía en varias líneas
    from textwrap import wrap

    if s.condiciones:
        c.setFont("Helvetica", 8)
        texto = s.condiciones.replace("\r", "").strip()
        lineas = wrap(texto, 110)  # ajustá el 110 si querés más/menos ancho

        text_obj = c.beginText()
        text_obj.setTextOrigin(10 * mm, 10 * mm)  # margen inferior izquierdo

        # Máximo de líneas visibles para no pisar el resto del contenido
        for linea in lineas[:6]:
            text_obj.textLine(linea)

        c.drawText(text_obj)

    c.showPage()
    c.save()
    buffer.seek(0)
    return send_file(
        buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"orden_{o.id}.pdf",
    )

@app.get("/orders/<int:order_id>/qr.png")
def order_qr_png(order_id: int):
    o = RepairOrder.query.get_or_404(order_id)
    public_url = url_for("order_public", token=o.token_publico, _external=True)
    img = qrcode.make(public_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")

@app.get("/t/<token>")
def order_public(token: str):
    o = RepairOrder.query.filter_by(token_publico=token).first_or_404()
    return render_template("public_order.html", order=o)

@app.get("/orders/<int:order_id>/ticket")
@login_required

def order_ticket(order_id: int):
    order = RepairOrder.query.get_or_404(order_id)
    return render_template("ticket.html", order=order)

