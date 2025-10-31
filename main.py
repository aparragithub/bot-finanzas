import os
import logging
from datetime import datetime
import base64

import os

# En Railway, las variables vienen directamente de os.getenv()
# No necesitamos load_dotenv()
try:
    from dotenv import load_dotenv
    load_dotenv()  # Para desarrollo local
except:
    pass


from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from groq import Groq
import gspread
from google.oauth2.service_account import Credentials
import json
import requests

# Importar módulos locales
from tasas import GestorTasas
from saldos import GestorSaldos

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# 🔑 DECODIFICAR CREDENCIALES DE RAILWAY
if os.getenv('GOOGLE_CREDENTIALS_B64'):
    try:
        creds_b64 = os.getenv('GOOGLE_CREDENTIALS_B64')
        creds_json = base64.b64decode(creds_b64).decode('utf-8')
        with open('google_credentials.json', 'w') as f:
            f.write(creds_json)
        logger.info("Credenciales de Google decodificadas")
    except Exception as e:
        logger.error(f"Error decodificando credenciales: {e}")


TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GROQ_API_KEY = os.getenv('GROQ_API_KEY')

groq_client = Groq(api_key=GROQ_API_KEY)
gestor_tasas = GestorTasas()  # Instancia global

def normalize_input(text: str) -> str:
    """Normaliza el input para mejorar compatibilidad sin acentos"""
    # Convertir a minúsculas
    normalized = text.lower()

    # Reemplazar variaciones sin acento con versiones acentuadas para referencia
    replacements = {
        'cambie ': 'cambié ',
        'cambie,': 'cambié,',
        'cambie.': 'cambié.',
        'gaste ': 'gasté ',
        'gaste,': 'gasté,',
        'gaste.': 'gasté.',
        'cobre ': 'cobré ',
        'cobre,': 'cobré,',
        'cobre.': 'cobré.',
        'compre ': 'compré ',
        'compre,': 'compré,',
        'compre.': 'compré.',
        'pague ': 'pagué ',
        'pague,': 'pagué,',
        'pague.': 'pagué.',
        'envie ': 'envié ',
        'envie,': 'envié,',
        'envie.': 'envié.',
    }

    for key, value in replacements.items():
        normalized = normalized.replace(key, value)

    return normalized

def get_google_sheets_client():
    """Obtiene el cliente de Google Sheets"""
    try:
        # Intenta usar Replit (en producción)
        hostname = os.getenv('REPLIT_CONNECTORS_HOSTNAME')
        if hostname:
            # Código Replit existente aquí
            x_replit_token = 'repl ' + os.getenv('REPL_IDENTITY', '')
            # ... resto código Replit
        else:
            # Localmente: usar archivo JSON
            credentials = Credentials.from_service_account_file(
                'google_credentials.json',
                scopes=[
                    'https://www.googleapis.com/auth/spreadsheets',
                    'https://www.googleapis.com/auth/drive'
                ]
            )
            gc = gspread.authorize(credentials)
            return gc
    except Exception as e:
        logger.error(f"Error al conectar Google Sheets: {e}")
        raise

def get_or_create_spreadsheet():
    """Obtiene o crea la hoja de cálculo de finanzas personales"""
    try:
        gc = get_google_sheets_client()

        try:
            spreadsheet = gc.open("Finanzas Personales - Bot")
        except gspread.SpreadsheetNotFound:
            spreadsheet = gc.create("Finanzas Personales - Bot")
            worksheet = spreadsheet.sheet1
            # Nuevos encabezados con Ubicación y Tasa
            worksheet.update('A1:I1', [['Fecha', 'Tipo', 'Categoría', 'Ubicación', 'Moneda', 'Monto', 'Tasa Usada', 'USD Equivalente', 'Descripción']])
            logger.info("Nueva hoja de cálculo creada")

        return spreadsheet
    except Exception as e:
        logger.error(f"Error al obtener/crear spreadsheet: {e}")
        raise

def classify_transaction(text: str) -> dict:
    """Usa Groq para clasificar la transacción con ubicación y moneda"""
    try:
        # Normalizar entrada
        normalized_text = normalize_input(text)

        prompt = f"""Eres un asistente EXPERTO en clasificar transacciones financieras personales. Tu respuesta DEBE ser JSON válido.

TEXTO A CLASIFICAR: "{normalized_text}"

INSTRUCCIÓN: Responde SOLO con JSON válido. SIN explicaciones, SIN markdown, SIN bloques de código.

ESTRUCTURA JSON REQUERIDA:
{{
    "tipo": "Ingreso" o "Egreso" o "Conversión",
    "categoria": una de las listadas abajo,
    "ubicacion": "Ecuador" o "Venezuela" o "Binance",
    "moneda": "USD" o "Bs" o "USDT",
    "monto": número positivo,
    "moneda_destino": string o null,
    "monto_destino": número o null,
    "descripcion": string breve
}}

CATEGORÍAS DISPONIBLES (BASADAS EN USO REAL):
1. "Sueldo" - Ingresos de trabajo
2. "Alimentación" - Comida, restaurantes, supermercado, delivery (Yummy, etc)
3. "Transporte" - Taxis, uber, traslados, gasolina
4. "Salud" - Seguros médicos, medicinas, doctores
5. "Servicios" - Celular, internet, agua, luz
6. "Comisión" - Comisiones bancarias, transferencias
7. "Compras" - Tarjetas de crédito (Multimax), compras en general
8. "Limpieza" - Artículos de limpieza, fundas, aseo
9. "IA" - Servicios de IA (Claude, ChatGPT, Groq)
10. "Conversión" - Cambio de moneda
11. "Saldo" - Registro de saldos iniciales
12. "Otro" - Lo que no encaje en las anteriores

PALABRAS CLAVE PARA MEJOR CLASIFICACIÓN:

ALIMENTACIÓN (incluye):
- comida, comida, almuerzo, desayuno, cena, comer
- restaurante, comedor, pizzería, pollería
- supermercado, mercado, tienda
- pan, leche, huevos, carnes
- delivery (Yummy, PedidosYa, UberEats, etc)
- cashe, cashea (app de crédito para comida)
- café, bebidas
- pollera de pollos, panadería

TRANSPORTE (incluye):
- taxi, uber, traslado
- gasolina, combustible
- yummy (cuando es SOLO traslado, no comida)
- moto, uber moto
- pasaje, boleto

SERVICIOS (incluye):
- celular, teléfono, movistar, digitel
- internet, wifi
- agua, acueducto
- luz, electricidad, corpoelec
- gas, sergas
- cable, tv

SALUD (incluye):
- seguro, médico, doctor, clínica
- medicina, farmacia, medicinas
- hospital, ambulancia
- odontólogo, dentista

COMISIÓN (incluye):
- comisión, comisiones
- pago móvil, transferencia bancaria
- retiro, depósito

COMPRAS (incluye):
- multimax, tarjeta de crédito
- deuda de tarjeta
- compra de bienes

LIMPIEZA (incluye):
- fundas, bolsas
- escoba, trapeador
- detergente, jabón
- limpieza, aseo
- artículos de limpieza

CONVERSIÓN (incluye):
- cambié, cambie, cambio
- convertí, convierte
- intercambié, intercambio
- traslado de dinero entre monedas

SALDO (incluye):
- saldo, saldo inicial
- ingreso de, recibí
- depósito inicial

PALABRAS CLAVE PARA TIPO:

CONVERSIÓN:
- cambié, cambie, cambio, convertí, convierte
- intercambié, intercambio, traslado
- por (seguido de número) - "cambié 100 por 95"

EGRESO:
- gasto, gaste, gasté, pagué, pague, pago
- compré, compre, compra
- pago de, deuda de
- envié, envie

INGRESO:
- ingreso, cobré, cobre, cobro
- sueldo, salario
- ganancia, recibí, recibe, recibo
- deposito, transferencia (entrante)
- saldo

REGLAS PARA UBICACIÓN (Muy Importante):

1. Si menciona "Bs" o "bolivar" → Venezuela, moneda Bs
2. Si menciona "usdt" o "binance" → Binance, moneda USDT
3. Si menciona "usd" o "ecuador" → Ecuador, moneda USD
4. Si menciona celular ecuatoriano (Movistar EC, Claro EC) → Ecuador
5. Si menciona aplicaciones venezolanas (Pago Móvil, BanCo) → Venezuela
6. Si NO especifica y es EGRESO → Asumir Ecuador (USD)
7. Si NO especifica y es INGRESO → Asumir Ecuador (USD)
8. Si NO especifica y es CONVERSIÓN:
   - Si destino es Bs → origen es USDT (Binance)
   - Si destino es USDT → origen es USD (Ecuador)

REGLAS ESPECIALES:

1. "Yummy" + número grande (100+) → Alimentación
2. "Yummy" + número pequeño (< 100) → Transporte
3. "Cashe/Cashea" → SIEMPRE Alimentación
4. "Multimax" → SIEMPRE Compras
5. "Fundas" → SIEMPRE Limpieza
6. "Corte de cabello" → Otro
7. "Traslado para..." → Transporte
8. "Gasto en..." → Depende contexto (comida=Alimentación, traslado=Transporte)

REGLAS PARA CONVERSIONES (CRÍTICO):
Si es CONVERSIÓN, SIEMPRE llenar moneda_destino y monto_destino:

DETECCIÓN AUTOMÁTICA DE MONEDA ORIGEN:
1. Si destino es "Bs" → origen es USDT (Binance → Venezuela)
2. Si destino es "USDT" → origen es USD (Ecuador → Binance)
3. Si no especifica origen pero monto_destino > 1000 y destino es Bs → origen es USDT

EJEMPLOS BASADOS EN TUS DATOS REALES:

1. "pago de seguro médico" 
   → {{tipo: "Egreso", categoria: "Salud", moneda: "Bs", ubicacion: "Venezuela", monto: 31487.62}}

2. "Gasto en pollera de pollos"
   → {{tipo: "Egreso", categoria: "Alimentación", moneda: "Bs", ubicacion: "Venezuela", monto: 7273.2}}

3. "Pago por yummy"
   → {{tipo: "Egreso", categoria: "Transporte", moneda: "Bs", ubicacion: "Venezuela", monto: 741.38}}

4. "Compra de fundas"
   → {{tipo: "Egreso", categoria: "Limpieza", moneda: "Bs", ubicacion: "Venezuela", monto: 3530}}

5. "Gasto en corte de cabello"
   → {{tipo: "Egreso", categoria: "Otro", moneda: "Bs", ubicacion: "Venezuela", monto: 1200}}

6. "pago de cuota cashe"
   → {{tipo: "Egreso", categoria: "Alimentación", moneda: "Bs", ubicacion: "Venezuela", monto: 7746.62}}

7. "pago deuda de celular de Ecuador"
   → {{tipo: "Egreso", categoria: "Servicios", moneda: "USD", ubicacion: "Ecuador", monto: 283.41}}

8. "Ingreso de sueldo"
   → {{tipo: "Ingreso", categoria: "Sueldo", moneda: "USD", ubicacion: "Ecuador", monto: 1467.91}}

9. "cambié 203.45 por 200 USDT"
   → {{tipo: "Conversión", moneda: "USD", monto: 203.45, moneda_destino: "USDT", monto_destino: 200}}

10. "cambié 49.71 USDT por 15000 Bs"
    → {{tipo: "Conversión", moneda: "USDT", monto: 49.71, moneda_destino: "Bs", monto_destino: 15000}}

VALIDACIÓN:
✓ monto DEBE ser número positivo
✓ Si tipo="Conversión": moneda_destino y monto_destino NO deben ser null
✓ descripcion DEBE describir claramente la transacción
✓ Categoría DEBE ser una de las 12 listadas
"""

        response = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": "Eres un asistente que clasifica transacciones financieras. Responde SOLO con JSON válido basado en datos reales."},
                {"role": "user", "content": prompt}
            ],
            model="llama-3.3-70b-versatile",
            temperature=0.2,  # Más bajo para más precisión
            max_tokens=400
        )

        result_text = response.choices[0].message.content.strip()

        # Limpiar markdown si está presente
        if result_text.startswith('```'):
            result_text = result_text.split('```')[1]
            if result_text.startswith('json'):
                result_text = result_text[4:]
            result_text = result_text.strip()

        result = json.loads(result_text)

        required_keys = ['tipo', 'categoria', 'ubicacion', 'moneda', 'monto', 'descripcion']
        if not all(key in result for key in required_keys):
            missing = [key for key in required_keys if key not in result]
            raise ValueError(f"Respuesta de IA incompleta. Faltan campos: {missing}")

        # Validar que monto sea un número válido
        try:
            monto = float(result['monto'])
            if monto <= 0:
                raise ValueError("El monto debe ser positivo")
            result['monto'] = monto
        except (ValueError, TypeError):
            raise ValueError(f"Monto inválido: {result.get('monto')}")

        # Agregar campos opcionales
        if 'moneda_destino' not in result:
            result['moneda_destino'] = None
        if 'monto_destino' not in result:
            result['monto_destino'] = None

        # Validar conversión
        if result['tipo'].lower() == 'conversión':
            if not result.get('moneda_destino') or result.get('moneda_destino') == '':
                raise ValueError("Para una conversión, debe especificar moneda_destino")
            if not result.get('monto_destino') or result.get('monto_destino') == 0:
                raise ValueError("Para una conversión, debe especificar monto_destino")
            try:
                result['monto_destino'] = float(result['monto_destino'])
            except (ValueError, TypeError):
                raise ValueError(f"monto_destino inválido: {result.get('monto_destino')}")

        logger.info(f"Clasificación exitosa: {result}")
        return result

    except Exception as e:
        logger.error(f"Error en clasificación: {e}")
        raise

def save_to_sheets(transaction_data: dict, tasa_usada: float = None) -> bool:
    """Guarda la transacción en Google Sheets con nueva estructura

    NOTA: Los egresos se guardan con signo NEGATIVO para simplificar cálculos
    Ejemplo:
    - Ingreso 1000 USD → Monto: 1000
    - Egreso 50 USD → Monto: -50
    - Conversión USD a USDT: Egreso -100, Ingreso +95
    """
    try:
        spreadsheet = get_or_create_spreadsheet()
        worksheet = spreadsheet.sheet1

        fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Convertir a USD
        monto_original = transaction_data['monto']
        moneda = transaction_data['moneda']
        tipo = transaction_data['tipo'].lower()

        # 🔑 APLICAR SIGNO NEGATIVO A EGRESOS
        if tipo == "egreso":
            monto_original = -abs(monto_original)
            monto_usd_multiplicador = -1
        else:
            monto_usd_multiplicador = 1

        # Convertir a USD
        # NOTA: Para USD/USDT, monto_original ya tiene el signo correcto
        # Para Bs, necesitamos aplicar el multiplicador
        if moneda == "Bs":
            if not tasa_usada:
                tasa_usada = gestor_tasas.obtener_tasa()
            if not tasa_usada:
                logger.error("No hay tasa para convertir Bs")
                return False
            monto_usd = (monto_original / tasa_usada) * monto_usd_multiplicador
        elif moneda in ["USD", "USDT"]:
            tasa_usada = 1.0
            monto_usd = monto_original  # YA TIENE EL SIGNO CORRECTO
        else:
            tasa_usada = 0
            monto_usd = 0

        row = [
            fecha,
            transaction_data['tipo'],
            transaction_data['categoria'],
            transaction_data['ubicacion'],
            moneda,
            monto_original,
            tasa_usada if tasa_usada else "",
            monto_usd,
            transaction_data['descripcion']
        ]

        worksheet.append_row(row)
        logger.info(f"Transacción guardada: {row}")
        return True

    except Exception as e:
        logger.error(f"Error al guardar en Sheets: {e}")
        return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start"""
    welcome_message = """Hola! 👋 Soy tu asistente de finanzas personales.

Envíame tus gastos e ingresos en lenguaje natural, por ejemplo:
• "gasto 50 bs comida"
• "ingreso 2000 sueldo"
• "gasté 15 usd uber"
• "cobrá 500 freelance venezuela"
• "cambié 102.24 usd a 100 usdt"

Yo me encargo de:
✅ Clasificar automáticamente cada transacción
✅ Convertir a USD usando la tasa BCV
✅ Guardar en Google Sheets
✅ Confirmar lo registrado

COMANDOS DISPONIBLES:
/start - Este mensaje
/help - Ayuda detallada
/tasa - Ver tasa BCV actual
/settasa 36.5 - Establecer tasa manual
/saldo - Ver portafolio completo
/saldo ecuador - Ver solo Ecuador
/saldo venezuela - Ver solo Venezuela
/saldo binance - Ver solo Binance

¡Empieza a registrar tus finanzas!"""

    await update.message.reply_text(welcome_message)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /help"""
    help_text = """📚 AYUDA - Bot de Finanzas Personales

📝 CÓMO USAR:
Escribe tus gastos o ingresos en lenguaje natural.

📝 EJEMPLOS:
• "gasto 50 bs comida" - Gasto en Bs en Venezuela
• "ingreso 2000 sueldo" - Ingreso en USD (Ecuador)
• "gasté 15 usd transporte ecuador" - Gasto en USD en Ecuador
• "gasto 100 usd comida venezuela" - Gasto en USD en Venezuela
• "cambié 102.24 usd a 100 usdt" - Conversión Binance
• "cobrá 500 freelance" - Ingreso adicional

📍 UBICACIONES:
• Ecuador - Para transacciones en Ecuador
• Venezuela - Para transacciones en Venezuela
• Binance - Para criptomonedas

💱 MONEDAS:
• USD - Dólar estadounidense
• Bs - Bolívar venezolano
• USDT - Tether (criptomoneda)

💵 CATEGORÍAS:
• Alimentación
• Transporte
• Servicios
• Entretenimiento
• Salud
• Educación
• Sueldo
• Freelance
• Inversiones
• Comisión
• Otros

🎯 COMANDOS ESPECIALES:
/tasa - Muestra la tasa BCV actual
/settasa 36.5 - Establece una tasa manual (override)
/saldo - Muestra tu portafolio completo en USD
/saldo ecuador - Solo saldo en Ecuador
/saldo venezuela - Solo saldo en Venezuela
/saldo binance - Solo saldo en Binance

Todas tus transacciones se guardan automáticamente en Google Sheets."""

    await update.message.reply_text(help_text)

async def comando_tasa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /tasa - Ver tasa BCV actual"""
    try:
        tasa = gestor_tasas.obtener_tasa()

        if not tasa:
            await update.message.reply_text("❌ No hay tasa disponible. Usa /settasa para establecerla.")
            return

        info = gestor_tasas.obtener_info()

        mensaje = f"💱 TASA BCV ACTUAL: {tasa:.2f} Bs/USD\n\n"

        if info['es_manual']:
            mensaje += "⚙️ Tasa MANUAL (override activado)\n"
        else:
            mensaje += "🔄 Tasa obtenida de API\n"

        mensaje += f"Última actualización: hace poco"

        await update.message.reply_text(mensaje)

    except Exception as e:
        logger.error(f"Error en comando /tasa: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def comando_settasa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /settasa 36.5 - Establecer tasa manual"""
    try:
        if not context.args:
            await update.message.reply_text("❌ Uso: /settasa 36.5")
            return

        tasa_str = context.args[0]

        if gestor_tasas.establecer_tasa_manual(float(tasa_str)):
            await update.message.reply_text(
                f"✅ Tasa establecida en: {float(tasa_str):.2f} Bs/USD\n\n"
                f"Se utilizará esta tasa para conversiones hasta cambiarla nuevamente."
            )
        else:
            await update.message.reply_text("❌ Error: Ingresa un número válido")

    except ValueError:
        await update.message.reply_text("❌ Error: Debes ingresar un número válido\nEjemplo: /settasa 36.5")
    except Exception as e:
        logger.error(f"Error en comando /settasa: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def comando_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /saldo - Ver portafolio"""
    try:
        spreadsheet = get_or_create_spreadsheet()
        gestor_saldos = GestorSaldos(spreadsheet.sheet1, gestor_tasas)

        if context.args:
            ubicacion = context.args[0]
            mensaje = gestor_saldos.obtener_saldo_por_ubicacion_formateado(ubicacion)
        else:
            mensaje = gestor_saldos.obtener_portafolio_detallado()

        await update.message.reply_text(mensaje)

    except Exception as e:
        logger.error(f"Error en comando /saldo: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Procesa mensajes de transacciones"""
    if not update.message or not update.message.text:
        return

    user_message = update.message.text

    await update.message.reply_text("🔄 Procesando tu transacción...")

    try:
        transaction = classify_transaction(user_message)

        # 🔑 VERIFICAR SI ES CONVERSIÓN
        if transaction['tipo'].lower() == 'conversión':
            # Validar que tenga moneda destino y monto destino
            if not transaction.get('moneda_destino') or not transaction.get('monto_destino'):
                await update.message.reply_text(
                    "❌ No pude detectar la conversión completa.\n\n"
                    "Usa el formato:\n"
                    "• 'cambié 125 por 120' (USD → USDT)\n"
                    "• 'cambié 120 por 2760 bs' (USDT → Bs)\n"
                    "• 'cambié 102.24 usd a 100 usdt' (explícito)"
                )
                return

            # Registrar egreso (origen)
            egreso_data = {
                'tipo': 'Egreso',
                'categoria': 'Conversión',
                'ubicacion': 'Ecuador' if transaction['moneda'] == 'USD' else 'Binance',
                'moneda': transaction['moneda'],
                'monto': transaction['monto'],
                'descripcion': f'Conversión a {transaction["moneda_destino"]}'
            }

            # Registrar ingreso (destino)
            ingreso_data = {
                'tipo': 'Ingreso',
                'categoria': 'Conversión',
                'ubicacion': 'Binance' if transaction['moneda_destino'] in ['USDT'] else 'Venezuela',
                'moneda': transaction['moneda_destino'],
                'monto': transaction['monto_destino'],
                'descripcion': f'Recibido de conversión ({transaction["moneda"]})'
            }

            # Guardar ambas transacciones
            success_egreso = save_to_sheets(egreso_data)
            success_ingreso = save_to_sheets(ingreso_data)

            if success_egreso and success_ingreso:
                # Calcular comisión
                comision_texto = "N/A"

                if transaction['moneda'] == 'USD' and transaction['moneda_destino'] == 'USDT':
                    comision = transaction['monto'] - transaction['monto_destino']
                    comision_pct = (comision / transaction['monto']) * 100
                    comision_texto = f"${comision:.2f} ({comision_pct:.2f}%)"
                elif transaction['moneda'] == 'USDT' and transaction['moneda_destino'] == 'BS':
                    tasa = gestor_tasas.obtener_tasa()
                    tasa_real = transaction['monto_destino'] / transaction['monto']
                    comision_texto = f"Tasa usada: {tasa_real:.2f} Bs/USD (Oficial: {tasa:.2f})"

                confirmation = f"""✅ ¡Conversión registrada!

📤 EGRESO:
   📍 {egreso_data['ubicacion']}
   💱 {transaction['moneda']}: {transaction['monto']}

📥 INGRESO:
   📍 {ingreso_data['ubicacion']}
   💱 {transaction['moneda_destino']}: {transaction['monto_destino']}

💸 Comisión: {comision_texto}

✅ 2 líneas guardadas en Google Sheets"""

                await update.message.reply_text(confirmation)
            else:
                await update.message.reply_text("❌ Error al guardar en Google Sheets. Intenta de nuevo.")

            return

        # 📝 PROCESAR TRANSACCIONES NORMALES (INGRESO/EGRESO)
        # Obtener tasa para conversión si es Bs
        tasa_para_guardar = None
        if transaction['moneda'] == "Bs":
            tasa_para_guardar = gestor_tasas.obtener_tasa()

        success = save_to_sheets(transaction, tasa_para_guardar)

        if success:
            # Determinar emoji según tipo
            if transaction['tipo'] == "Ingreso":
                tipo_emoji = "💰"
            else:
                tipo_emoji = "💸"

            # Calcular USD equivalente
            if transaction['moneda'] == "Bs" and tasa_para_guardar:
                monto_usd = transaction['monto'] / tasa_para_guardar
            elif transaction['moneda'] in ["USD", "USDT"]:
                monto_usd = transaction['monto']
            else:
                monto_usd = 0

            confirmation = f"""{tipo_emoji} ¡Registrado!

📍 Ubicación: {transaction['ubicacion']}
💳 Tipo: {transaction['tipo']}
🏷️ Categoría: {transaction['categoria']}
💱 Moneda: {transaction['moneda']}
💵 Monto: {transaction['monto']:.2f}
📊 USD Equivalente: ${monto_usd:.2f}
📝 Descripción: {transaction['descripcion']}

✅ Guardado en Google Sheets"""

            await update.message.reply_text(confirmation)
        else:
            await update.message.reply_text("❌ Error al guardar en Google Sheets. Intenta de nuevo.")

    except Exception as e:
        logger.error(f"Error procesando mensaje: {e}")
        await update.message.reply_text(
            "❌ No pude procesar tu mensaje. Intenta con:\n\n"
            "GASTOS/INGRESOS:\n"
            "• 'gasto 50 bs comida' (o sin acento: 'gaste')\n"
            "• 'ingreso 2000 sueldo'\n"
            "• 'gasté 15 usd transporte' (o: 'gaste')\n"
            "• 'cobré 500 freelance' (o: 'cobre')\n\n"
            "CONVERSIONES:\n"
            "• 'cambié 125 por 120' (USD→USDT, o: 'cambie')\n"
            "• 'cambié 126.5 por 125' (o: 'cambie')\n"
            "• 'cambié 120 por 2760 bs' (o: 'cambie')\n"
            "• 'cambié 102.24 usd a 100 usdt' (o: 'cambie')"
        )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manejo de errores global"""
    logger.error(f"Update {update} caused error {context.error}")

def main():
    """Función principal"""
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN no encontrado")
        return

    if not GROQ_API_KEY:
        logger.error("GROQ_API_KEY no encontrado")
        return

    logger.info("Iniciando bot de finanzas personales (v2 con múltiples monedas)...")

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Comandos
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("tasa", comando_tasa))
    application.add_handler(CommandHandler("settasa", comando_settasa))
    application.add_handler(CommandHandler("saldo", comando_saldo))

    # Mensajes
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Error handler
    application.add_error_handler(error_handler)

    logger.info("Bot iniciado. Esperando mensajes...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()