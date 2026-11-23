from flask import Flask, jsonify, render_template, request, session, redirect, url_for
import sqlite3
import math
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
# Llave secreta para firmar las cookies de sesión
app.secret_key = 'llave_secreta_laboratorio_iot'

# Contraseña maestra adicional para desbloquear la pestaña "Hogar"
CLAVE_MAESTRA_HOGAR = "admin123"

# ==========================================
# 1. INICIALIZACIÓN Y CONSULTAS DE BASE DE DATOS
# ==========================================

def inicializar_tablas_servidor():
    conexion = sqlite3.connect('datos.db')
    cursor = conexion.cursor()

    # Tabla de lecturas sísmicas (Nodo 4G)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS lecturas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            accel_x REAL NOT NULL,
            accel_y REAL NOT NULL,
            accel_z REAL NOT NULL
        )
    ''')

    # Tabla de lecturas del nodo interior (cerebro_central.py)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS lecturas_interior (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            temperatura REAL,
            humedad REAL,
            gas_adc INTEGER,
            fuga_gas INTEGER,
            lluvia INTEGER,
            golpe_calor INTEGER
        )
    ''')

    # Tabla de usuarios con contraseñas hasheadas
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            rol TEXT DEFAULT 'Familiar',
            creado_en DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Tabla de Logs de acceso a la plataforma
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS logs_acceso (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            ip TEXT,
            evento TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Crear usuario administrador por defecto si la tabla está vacía (admin / admin123)
    cursor.execute("SELECT COUNT(*) FROM usuarios")
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "INSERT INTO usuarios (username, password_hash, rol) VALUES (?, ?, ?)",
            ("admin", generate_password_hash("admin123"), "Administrador")
        )

    conexion.commit()
    conexion.close()

def registrar_log_acceso(username, evento="Ingreso exitoso"):
    try:
        ip_cliente = request.remote_addr or "127.0.0.1"
        conexion = sqlite3.connect('datos.db')
        cursor = conexion.cursor()
        cursor.execute(
            "INSERT INTO logs_acceso (username, ip, evento) VALUES (?, ?, ?)",
            (username, ip_cliente, evento)
        )
        conexion.commit()
        conexion.close()
    except Exception as e:
        print(f"Error registrando log: {e}")

def verificar_credenciales(username, password):
    conexion = sqlite3.connect('datos.db')
    conexion.row_factory = sqlite3.Row
    cursor = conexion.cursor()
    cursor.execute("SELECT password_hash FROM usuarios WHERE username = ?", (username,))
    usuario = cursor.fetchone()
    conexion.close()

    if usuario and check_password_hash(usuario['password_hash'], password):
        return True
    return False

def obtener_estado_completo():
    conexion = sqlite3.connect('datos.db')
    conexion.row_factory = sqlite3.Row
    cursor = conexion.cursor()

    # 1. Última lectura del Nodo Interior (DHT11 + MQ)
    cursor.execute("""
        SELECT strftime('%H:%M:%S', timestamp, 'localtime') as hora,
               temperatura, humedad, gas_adc, fuga_gas, lluvia, golpe_calor
        FROM lecturas_interior
        ORDER BY id DESC LIMIT 1
    """)
    row_int = cursor.fetchone()

    if row_int:
        interior = {
            "hora": row_int["hora"],
            "temp": round(float(row_int["temperatura"]), 1),
            "hum": round(float(row_int["humedad"]), 1),
            "gas_adc": int(row_int["gas_adc"]),
            "fuga_gas": bool(row_int["fuga_gas"]),
            "lluvia": bool(row_int["lluvia"]),
            "golpe_calor": bool(row_int["golpe_calor"])
        }
    else:
        interior = {
            "hora": "--:--:--",
            "temp": 22.0,
            "hum": 55.0,
            "gas_adc": 0,
            "fuga_gas": False,
            "lluvia": False,
            "golpe_calor": False
        }

    # 2. Últimos 6 eventos telúricos (Nodo Sísmico 4G)
    cursor.execute("""
        SELECT id, strftime('%H:%M:%S', timestamp, 'localtime') as hora,
               accel_x, accel_y, accel_z
        FROM lecturas
        ORDER BY id DESC LIMIT 6
    """)
    rows_sismo = cursor.fetchall()

    eventos_sismicos = []
    pga_actual = 0.0

    for idx, r in enumerate(rows_sismo):
        ax = float(r["accel_x"])
        ay = float(r["accel_y"])
        az = float(r["accel_z"])
        mag_ms2 = math.sqrt(ax**2 + ay**2 + az**2)
        pga_g = round(abs(mag_ms2 - 9.81) / 9.81, 3) if mag_ms2 > 4.0 else round(mag_ms2, 3)

        if idx == 0:
            pga_actual = pga_g

        if pga_g >= 0.150:
            nivel_fila = "Riesgo Alto"
        elif pga_g >= 0.050:
            nivel_fila = "Riesgo Moderado"
        elif pga_g >= 0.015:
            nivel_fila = "Riesgo Leve"
        else:
            nivel_fila = "Todo bien"

        eventos_sismicos.append({
            "id": r["id"],
            "hora": r["hora"],
            "x": round(ax, 2),
            "y": round(ay, 2),
            "z": round(az, 2),
            "pga": pga_g,
            "nivel": nivel_fila
        })

    # Clasificación del estado sísmico actual
    hay_terremoto = False
    if pga_actual >= 0.150:
        estado_sismo = "Riesgo Alto"
        codigo_riesgo = "alto"
        hay_terremoto = True
    elif pga_actual >= 0.050:
        estado_sismo = "Riesgo Moderado"
        codigo_riesgo = "moderado"
        hay_terremoto = True
    elif pga_actual >= 0.015:
        estado_sismo = "Riesgo Leve"
        codigo_riesgo = "leve"
    else:
        estado_sismo = "Todo bien"
        codigo_riesgo = "bien"

    # 3. Determinar estado de Puerta y Ventana según la Matriz de Prioridades
    if hay_terremoto:
        estado_ventana = "Abierta"
        estado_puerta = "Abierta"
    elif interior["fuga_gas"] and interior["lluvia"]:
        estado_ventana = "Cerrada"
        estado_puerta = "Abierta"
    elif interior["fuga_gas"]:
        estado_ventana = "Abierta"
        estado_puerta = "Cerrada"
    elif interior["golpe_calor"]:
        estado_ventana = "Cerrada"
        estado_puerta = "Cerrada"
    elif interior["lluvia"]:
        estado_ventana = "Cerrada"
        estado_puerta = "Cerrada"
    else:
        estado_ventana = "Abierta"
        estado_puerta = "Cerrada"

    # 4. Últimos 15 Logs de Acceso
    cursor.execute("""
        SELECT id, username, ip, evento,
               strftime('%Y-%m-%d %H:%M:%S', timestamp, 'localtime') as fecha_hora
        FROM logs_acceso
        ORDER BY id DESC LIMIT 15
    """)
    logs = [dict(row) for row in cursor.fetchall()]

    conexion.close()

    return {
        "interior": interior,
        "accesos": {
            "puerta": estado_puerta,
            "ventana": estado_ventana
        },
        "sismo": {
            "estado": estado_sismo,
            "codigo": codigo_riesgo,
            "pga": pga_actual,
            "activo": hay_terremoto,
            "historial": eventos_sismicos
        },
        "logs": logs
    }

# ==========================================
# 2. RUTAS DE AUTENTICACIÓN DIRECTA (SIN 2FA)
# ==========================================

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        if verificar_credenciales(username, password):
            session['autenticado'] = True
            session['usuario'] = username
            registrar_log_acceso(username, "Acceso exitoso (Hash + HTTPS)")
            return redirect(url_for('index'))
        else:
            registrar_log_acceso(username or "Desconocido", "Intento fallido de login")
            error = "Credenciales incorrectas o acceso no autorizado."

    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    usuario = session.get('usuario', 'Usuario')
    registrar_log_acceso(usuario, "Cierre de sesión")
    session.clear()
    return redirect(url_for('login'))

# ==========================================
# 3. RUTAS DEL DASHBOARD, HOGAR Y LOGS
# ==========================================

@app.route('/')
def index():
    if not session.get('autenticado'):
        return redirect(url_for('login'))
    return render_template('index.html', usuario=session.get('usuario', 'Admin'))

@app.route('/api/estado_general')
def api_estado_general():
    if not session.get('autenticado'):
        return jsonify({"error": "No autorizado"}), 401
    return jsonify(obtener_estado_completo())

@app.route('/api/hogar/desbloquear', methods=['POST'])
def desbloquear_hogar():
    if not session.get('autenticado'):
        return jsonify({"error": "No autorizado"}), 401

    data = request.get_json() or {}
    password = data.get('password', '')
    usuario_actual = session.get('usuario', 'admin')

    if password == CLAVE_MAESTRA_HOGAR or verificar_credenciales(usuario_actual, password):
        session['hogar_desbloqueado'] = True
        conexion = sqlite3.connect('datos.db')
        conexion.row_factory = sqlite3.Row
        cursor = conexion.cursor()
        cursor.execute("SELECT id, username, rol, strftime('%Y-%m-%d', creado_en) as fecha FROM usuarios ORDER BY id ASC")
        usuarios = [dict(r) for r in cursor.fetchall()]
        conexion.close()
        return jsonify({"ok": True, "usuarios": usuarios})

    return jsonify({"ok": False, "error": "Contraseña incorrecta"}), 403

@app.route('/api/hogar/agregar_usuario', methods=['POST'])
def agregar_usuario_hogar():
    if not session.get('autenticado') or not session.get('hogar_desbloqueado'):
        return jsonify({"ok": False, "error": "Pestaña Hogar bloqueada"}), 403

    data = request.get_json() or {}
    nuevo_user = data.get('username', '').strip()
    nueva_pass = data.get('password', '').strip()
    rol = data.get('rol', 'Familiar').strip()

    if len(nuevo_user) < 3 or len(nueva_pass) < 4:
        return jsonify({"ok": False, "error": "Usuario (mín. 3 letras) y contraseña (mín. 4 caracteres) requeridos."}), 400

    try:
        conexion = sqlite3.connect('datos.db')
        conexion.row_factory = sqlite3.Row
        cursor = conexion.cursor()
        cursor.execute(
            "INSERT INTO usuarios (username, password_hash, rol) VALUES (?, ?, ?)",
            (nuevo_user, generate_password_hash(nueva_pass), rol)
        )
        conexion.commit()
        registrar_log_acceso(session.get('usuario', 'admin'), f"Creó cuenta familiar: {nuevo_user}")

        cursor.execute("SELECT id, username, rol, strftime('%Y-%m-%d', creado_en) as fecha FROM usuarios ORDER BY id ASC")
        usuarios = [dict(r) for r in cursor.fetchall()]
        conexion.close()
        return jsonify({"ok": True, "mensaje": f"Familiar '{nuevo_user}' añadido con éxito.", "usuarios": usuarios})
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "error": "Ese nombre de usuario ya existe."}), 400

# ==========================================
# 4. EJECUCIÓN DEL SERVIDOR CON TÚNEL HTTPS
# ==========================================
if __name__ == '__main__':
    inicializar_tablas_servidor()
    app.run(host='0.0.0.0', port=5000, ssl_context=('cert.pem', 'key.pem'), debug=True)
