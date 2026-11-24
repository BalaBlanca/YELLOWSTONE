#include <Arduino.h>
#include <Wire.h>

// ==========================================
// 1. PINES UART MÓDEM A7670SA E I2C MPU-6050
// ==========================================
#define MODEM_RX   18       // Conectar a T-TXD del A7670SA
#define MODEM_TX   17       // Conectar a R-RXD del A7670SA
#define MODEM_BAUD 115200

#define I2C_SDA    21       // SDA del MPU-6050
#define I2C_SCL    22       // SCL del MPU-6050
#define MPU_ADDR   0x68     // Dirección I2C estándar del MPU-6050

// ==========================================
// 2. RED CELULAR Y BROKER MQTT (RASPBERRY PI)
// ==========================================
const char APN[]         = "web.vmovil.co";

// IP numérica y puerto fijo de tu túnel Pinggy Pro
const char MQTT_BROKER[] = "";
const char CLIENT_ID[]   = "Nodo_Sismico_4G_01";
const char MQTT_USER[]   = "pi_listener";
const char MQTT_PASS[]   = "";
const char TOPIC_SISMO[] = "sensores/mpu6050";

// ==========================================
// 3. TEMPORIZADORES AUTOMÁTICOS (MILLIS)
// ==========================================
const uint32_t INTERVALO_ENVIO_MS  = 1000;  // Enviar datos cada 1 segundo (1000 ms)
const uint32_t INTERVALO_RECONN_MS = 90000; // Ejecutar RECONN preventivo cada 1.5 minutos (90s)

uint32_t ultimoEnvioMs  = 0;
uint32_t ultimoReconnMs = 0;
bool mqttConectado      = false;

HardwareSerial ModemSerial(1);

// ==========================================
// 4. LECTURA DIRECTA DEL SENSOR MPU-6050 (I2C)
// ==========================================
void inicializarMPU6050() {
  Wire.begin(I2C_SDA, I2C_SCL);
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x6B); // Registro PWR_MGMT_1
  Wire.write(0x00); // Despertar al MPU-6050
  if (Wire.endTransmission(true) == 0) {
    Serial.println("✅ Sensor MPU-6050 inicializado correctamente en I2C (0x68).");
  } else {
    Serial.println("⚠️ Advertencia: No se detectó el MPU-6050 en GPIO 21/22.");
  }

  // Configurar rango del acelerómetro en ±2g (16384 LSB/g)
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x1C); // Registro ACCEL_CONFIG
  Wire.write(0x00);
  Wire.endTransmission(true);
}

void leerAceleracionMPU(float &ax_ms2, float &ay_ms2, float &az_ms2) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x3B); // Registro inicial ACCEL_XOUT_H
  if (Wire.endTransmission(false) != 0) {
    ax_ms2 = 0.0;
    ay_ms2 = 0.0;
    az_ms2 = 9.81;
    return;
  }

  Wire.requestFrom((uint8_t)MPU_ADDR, (size_t)6, true);
  if (Wire.available() == 6) {
    int16_t rawX = (Wire.read() << 8) | Wire.read();
    int16_t rawY = (Wire.read() << 8) | Wire.read();
    int16_t rawZ = (Wire.read() << 8) | Wire.read();

    // Convertir de cuentas raw (±2g = 16384 LSB/g) a m/s^2 (1g = 9.81 m/s^2)
    ax_ms2 = (rawX / 16384.0f) * 9.81f;
    ay_ms2 = (rawY / 16384.0f) * 9.81f;
    az_ms2 = (rawZ / 16384.0f) * 9.81f;
  } else {
    ax_ms2 = 0.0;
    ay_ms2 = 0.0;
    az_ms2 = 9.81;
  }
}

// ==========================================
// 5. FUNCIONES AT Y ALGORITMO DE RECONEXIÓN
// ==========================================
bool sendATCommand(const char* cmd, const char* expectedAns, uint32_t timeoutMs = 3000) {
  while (ModemSerial.available()) ModemSerial.read();
  if (strlen(cmd) > 0) {
    Serial.print("\n[TX AT] -> ");
    Serial.println(cmd);
    ModemSerial.println(cmd);
  }

  String response = "";
  uint32_t start = millis();
  while (millis() - start < timeoutMs) {
    while (ModemSerial.available()) {
      char c = ModemSerial.read();
      response += c;
      Serial.write(c);
    }
    if (response.indexOf(expectedAns) != -1) {
      return true;
    }
  }
  return false;
}

void esperarArranqueModem() {
  Serial.println("\n>>> Esperando a que el A7670SA esté listo... <<<");
  for (int i = 0; i < 20; i++) {
    if (sendATCommand("AT", "OK", 1000)) {
      Serial.println("\n>>> ¡Módulo A7670SA activo! Fijando modo 4G LTE... <<<");
      sendATCommand("AT+CNMP=38", "OK", 2000);
      delay(2000);
      return;
    }
    delay(500);
  }
}

bool conectarBrokerRPi() {
  Serial.println("\n🔄 [RECONN] Iniciando secuencia de conexión/reconexión 4G MQTT...");

  // 1. Limpiar sesiones MQTT previas para evitar bloqueos al hacer RECONN cada 1.5 min
  sendATCommand("AT+CMQTTDISC=0,60", "OK", 2000);
  sendATCommand("AT+CMQTTREL=0", "OK", 2000);
  sendATCommand("AT+CMQTTSTOP", "OK", 2000);

  // 2. Verificar SIM y registro celular
  bool simLista = false;
  for (int i = 0; i < 10; i++) {
    if (sendATCommand("AT+CPIN?", "READY", 2000)) {
      simLista = true;
      break;
    }
    delay(1000);
  }
  if (!simLista) {
    mqttConectado = false;
    return false;
  }

  bool registrado = false;
  for (int i = 0; i < 15; i++) {
    while (ModemSerial.available()) ModemSerial.read();
    ModemSerial.println("AT+CGREG?");
    delay(1000);
    String resp = "";
    while (ModemSerial.available()) resp += (char)ModemSerial.read();
    Serial.print(resp);
    if (resp.indexOf(",1") != -1 || resp.indexOf(",5") != -1) {
      registrado = true;
      break;
    }
    delay(1000);
  }
  if (!registrado) {
    mqttConectado = false;
    return false;
  }

  // 3. Configurar APN y abrir red de datos
  char apnCmd[128];
  snprintf(apnCmd, sizeof(apnCmd), "AT+CGDCONT=1,\"IP\",\"%s\"", APN);
  sendATCommand(apnCmd, "OK", 3000);

  if (!sendATCommand("AT+NETOPEN", "+NETOPEN: 0", 8000)) {
    sendATCommand("AT+NETOPEN?", "+NETOPEN: 1", 3000);
  }

  // 4. Iniciar pila MQTT y adquirir cliente
  sendATCommand("AT+CMQTTSTART", "+CMQTTSTART: 0", 5000);

  char accqCmd[64];
  snprintf(accqCmd, sizeof(accqCmd), "AT+CMQTTACCQ=0,\"%s\"", CLIENT_ID);
  sendATCommand(accqCmd, "OK", 3000);

  // 5. Conectar con usuario y contraseña (pi_listener / 5678)
  char connCmd[200];
  snprintf(connCmd, sizeof(connCmd),
           "AT+CMQTTCONNECT=0,\"%s\",90,1,\"%s\",\"%s\"",
           MQTT_BROKER, MQTT_USER, MQTT_PASS);

  if (sendATCommand(connCmd, "+CMQTTCONNECT: 0,0", 20000)) {
    Serial.println("\n✅ ¡CONECTADO EXITOSAMENTE AL BROKER DE LA RASPBERRY PI!");
    mqttConectado = true;
    ultimoReconnMs = millis(); // Reiniciar contador de 1.5 minutos
    return true;
  } else {
    Serial.println("\n❌ Falló la conexión con el Broker.");
    mqttConectado = false;
    ultimoReconnMs = millis();
    return false;
  }
}

void enviarLecturaMPU6050() {
  float ax, ay, az;
  leerAceleracionMPU(ax, ay, az);

  String payload = String("{\"x\":") + String(ax, 2) +
                   ",\"y\":" + String(ay, 2) +
                   ",\"z\":" + String(az, 2) + "}";

  char cmd[64];

  snprintf(cmd, sizeof(cmd), "AT+CMQTTTOPIC=0,%d", strlen(TOPIC_SISMO));
  if (!sendATCommand(cmd, ">", 1500)) {
    mqttConectado = false;
    return;
  }
  ModemSerial.print(TOPIC_SISMO);
  if (!sendATCommand("", "OK", 1500)) {
    mqttConectado = false;
    return;
  }

  snprintf(cmd, sizeof(cmd), "AT+CMQTTPAYLOAD=0,%d", payload.length());
  if (!sendATCommand(cmd, ">", 1500)) {
    mqttConectado = false;
    return;
  }
  ModemSerial.print(payload);
  if (!sendATCommand("", "OK", 1500)) {
    mqttConectado = false;
    return;
  }

  // QoS 0 (AT+CMQTTPUB=0,0,60) para envío rápido cada 1 segundo sin bloqueo por PUBACK
  if (sendATCommand("AT+CMQTTPUB=0,0,60", "+CMQTTPUB: 0,0", 5000)) {
    Serial.printf("\n🚀 [4G TX -> RPi] %s\n", payload.c_str());
  } else {
    Serial.println("\n⚠️ Error al publicar. Activando RECONN automático...");
    mqttConectado = false;
  }
}

// ==========================================
// 6. SETUP Y LOOP PRINCIPAL
// ==========================================
void setup() {
  Serial.begin(115200);
  delay(2000);

  inicializarMPU6050();
  ModemSerial.begin(MODEM_BAUD, SERIAL_8N1, MODEM_RX, MODEM_TX);

  esperarArranqueModem();
  conectarBrokerRPi();

  ultimoEnvioMs  = millis();
  ultimoReconnMs = millis();
}

void loop() {
  uint32_t ahora = millis();

  // 1. Algoritmo de RECONN periódico cada 1.5 minutos (90,000 ms) o si se cayó el enlace
  if (!mqttConectado || (ahora - ultimoReconnMs >= INTERVALO_RECONN_MS)) {
    Serial.println("\n⏱️ Ejecutando ciclo automático de RECONN (1.5 min)...");
    conectarBrokerRPi();
    ultimoEnvioMs = millis();
  }

  // 2. Envío automático de lecturas del MPU-6050 cada 1 segundo (1,000 ms)
  if (mqttConectado && (ahora - ultimoEnvioMs >= INTERVALO_ENVIO_MS)) {
    ultimoEnvioMs = ahora;
    enviarLecturaMPU6050();
  }

  // 3. Permitir comandos manuales desde el Monitor Serial por si deseas forzar PUB o RECONN
  if (Serial.available()) {
    String input = Serial.readStringUntil('\n');
    input.trim();
    if (input.equalsIgnoreCase("PUB")) {
      enviarLecturaMPU6050();
    } else if (input.equalsIgnoreCase("RECONN")) {
      conectarBrokerRPi();
    } else if (input.length() > 0) {
      ModemSerial.println(input);
    }
  }
}
