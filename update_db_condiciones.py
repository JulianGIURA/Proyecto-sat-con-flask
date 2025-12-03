"""
Script para agregar el campo 'condiciones' a la tabla 'settings' en sat.db

Uso:
    1) Guardar este archivo como: update_db_condiciones.py
    2) Copiarlo a la misma carpeta donde está sat.db
    3) Ejecutar desde una terminal/cmd:
           python update_db_condiciones.py
"""

import sqlite3
import os
import shutil

DB_FILENAME = "sat.db"
BACKUP_FILENAME = "sat_backup_before_condiciones.db"


def main():
    if not os.path.exists(DB_FILENAME):
        print(f"No se encontró {DB_FILENAME} en esta carpeta.")
        return

    # Backup de seguridad
    if not os.path.exists(BACKUP_FILENAME):
        shutil.copy(DB_FILENAME, BACKUP_FILENAME)
        print(f"Copia de seguridad creada: {BACKUP_FILENAME}")
    else:
        print(f"Ya existe un backup: {BACKUP_FILENAME}")

    conn = sqlite3.connect(DB_FILENAME)
    cur = conn.cursor()

    # Ver columnas actuales de 'settings'
    cur.execute("PRAGMA table_info(settings)")
    cols = cur.fetchall()
    col_names = [c[1] for c in cols]
    print("Columnas actuales en 'settings':", col_names)

    # Si ya existe, no hacemos nada
    if "condiciones" in col_names:
        print("La columna 'condiciones' ya existe. No se hace nada.")
        conn.close()
        return

    # Agregar la columna
    print("Agregando columna 'condiciones' a la tabla 'settings'...")
    cur.execute("ALTER TABLE settings ADD COLUMN condiciones TEXT")
    conn.commit()

    # Verificar
    cur.execute("PRAGMA table_info(settings)")
    cols_after = cur.fetchall()
    col_names_after = [c[1] for c in cols_after]
    print("Columnas después del cambio:", col_names_after)

    conn.close()
    print("Listo. Ahora podés usar el campo 'condiciones' en tu modelo Settings.")


if __name__ == "__main__":
    main()
