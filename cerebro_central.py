import json
import sqlite3
from datetime import datetime
import paho.mqtt.client as mqtt

BROKER_IP   = "localhost"
BROKER_PORT = 1883

# ==========================================
# UMBRALES DEL ALGORITMO DE DECISIÓN
# ==========================================
UMBRAL_GAS_ADC        = 1600  # Rango ADC ESP32-S3: 0 a 4095 (Ajustar según calibración del MQ)
UMBRAL_TEMP_CALOR_C   = 30.0  # Temperatura en °C para considerar golpe de calor
UMBRAL_HUM_LLUVIA_PCT = 82.0  # Humedad relativa % para inferir lluvia (o usar tópico externo)
UMBRAL_SISMO_PGA_G    = 0.050 # Aceleración en g del nodo remoto 4G para disparar terremoto

# ==========================================
# BASE DE DATOS SQLITE (NODO INTERIOR + ALERTAS)
# ==========================================
def inicializar_db():
    conn = sqlite3.connect('datos.db')
    cursor = conn.cursor()
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
    conn.commit()
    conn.close()

def registrar_interior_db(temp, hum, gas, e_gas, e_lluvia, e_calor):
    conn = sqlite3.connect('datos.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO lecturas_interior 
        (temperatura, humedad, gas_adc, fuga_gas, lluvia, golpe_calor)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (temp, hum, gas, e_gas, e_lluvia, e_calor))
    conn.commit()
    conn.close()

# ==========================================
# LÓGICA DE EVALUACIÓN Y RESPUESTA MQTT
# ==========================================
def evaluar_nodo_interior(client, datos):
    temp = float(datos.get("temp", 0.0))
    hum  = float(datos.get("hum", 0.0))
    gas  = int(datos.get("gas", 0))

    # 1. Determinación de fenómenos según umbrales
    fuga_gas    = 1 if gas >= UMBRAL_GAS_ADC else 0
    lluvia      = 1 if hum >= UMBRAL_HUM_LLUVIA_PCT else 0
    golpe_calor = 1 if temp >= UMBRAL_TEMP_CALOR_C else 0

    # 2. Publicar órdenes de acción hacia la ESP32-S3 interior
    client.publish("fuga_de_gas", str(fuga_gas), retain=True)
    client.publish("lluvia", str(lluvia), retain=True)
    client.publish("golpe_de_calor", str(golpe_calor), retain=True)
    client.publish("golpe de calor", str(golpe_calor), retain=True)

    # 3. Guardar en la base de datos para la interfaz web
    registrar_interior_db(temp, hum, gas, fuga_gas, lluvia, golpe_calor)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🏠 Interior -> "
          f"T:{temp}°C | H:{hum}% | Gas:{gas}  ==>  "
          f"Alertas [Gas:{fuga_gas}, Lluvia:{lluvia}, Calor:{golpe_calor}]")

def evaluar_nodo_sismico(client, datos):
    # Soporta tanto el tópico sensores/alerta como sensores/mpu6050 del módulo 4G
    alerta_directa = datos.get("estado_logico", None)
    pga            = float(datos.get("pga_g", datos.get("pga", 0.0)))

    if alerta_directa is not None:
        hay_terremoto = 1 if int(alerta_directa) == 1 else 0
    else:
        hay_terremoto = 1 if pga >= UMBRAL_SISMO_PGA_G else 0

    client.publish("terremoto", str(hay_terremoto), retain=True)
    print(f"🚨 [ALERTA SÍSMICA] PGA: {pga}g -> Enviando a tópico 'terremoto': {hay_terremoto}")

# ==========================================
# CALLBACKS MQTT
# ==========================================
def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print("✅ Cerebro Central conectado al Broker local (localhost:1883)")
        client.subscribe("casa/sensores/interior")
        client.subscribe("sensores/alerta")
        client.subscribe("sensores/mpu6050")
    else:
        print(f"❌ Error al conectar: {rc}")

def on_message(client, userdata, msg):
    try:
        payload = msg.payload.decode('utf-8')
        datos = json.loads(payload)

        if msg.topic == "casa/sensores/interior":
            evaluar_nodo_interior(client, datos)
        elif msg.topic in ["sensores/alerta", "sensores/mpu6050"]:
            evaluar_nodo_sismico(client, datos)

    except Exception as e:
        print(f"⚠️ Error procesando mensaje en {msg.topic}: {e}")

if __name__ == '__main__':
    inicializar_db()
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        client = mqtt.Client()
        
    client.username_pw_set("pi_listener", "5678")


    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER_IP, BROKER_PORT, 60)
    client.loop_forever()
