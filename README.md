# SAT Flask v3
- Configuración con **logo**, datos de empresa y política (`/settings`)
- PDF con **logo** + QR (`/orders/<id>/pdf`)
- **Caja** (entradas/salidas) con saldo (`/cash`, `/cash/new`)
- **Repuestos internos** por orden (no visibles públicamente)
- Botón **Compartir por WhatsApp** desde el detalle
- Gestión de **usuarios y roles** (admin/tecnico/cajero) solo para administradores

Accesos de administrador
-----------------------
- Usuario por defecto: `admin` / `admin` (se crea automáticamente si no existe).
- Menú de administración para crear, editar o eliminar usuarios en `/users`.
- Los administradores no pueden eliminarse a sí mismos ni borrar el último admin.

Instalar:
```bash
pip install -r requirements.txt
export FLASK_APP=app.py
flask run
```
Datos demo:
```bash
flask --app app.py seed
```
