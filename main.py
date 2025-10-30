import os
import logging
from datetime import datetime
from dotenv import load_dotenv  # ← AGREGAR ESTO

# Cargar variables del .env
load_dotenv()  # ← AGREGAR ESTO

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

        prompt = f"""Eres un asistente EXPERTO en clasificar transacciones financieras. Tu respuesta DEBE ser JSON válido.

TEXTO A CLASIFICAR: "{normalized_text}"

INSTRUCCIÓN: Responde SOLO con JSON válido. SIN explicaciones, SIN markdown, SIN bloques de código.

ESTRUCTURA JSON REQUERIDA:
{{
    "tipo": "Ingreso" o "Egreso" o "Conversión",
    "categoria": una de: "Alimentación", "Transporte", "Servicios", "Entretenimiento", "Salud", "Educación", "Sueldo", "Freelance", "Inversiones", "Comisión", "Otros",
    "ubicacion": "Ecuador" o "Venezuela" o "Binance",
    "moneda": "USD" o "Bs" o "USDT",
    "monto": número positivo,
    "moneda_destino": string o null,
    "monto_destino": número o null,
    "descripcion": string breve
}}

PALABRAS CLAVE PARA TIPO:
- CONVERSIÓN: cambié, cambie, cambio, convertí, convierte, intercambié, intercambio, traslado, cambiaste
- EGRESO: gasto, gaste, gasté, compré, compre, compra, pagué, pague, pago, envié, envie, envio
- INGRESO: ingreso, cobré, cobre, cobro, sueldo, ganancia, recibí, recibe, recibo, deposito, transferencia (entrante)

REGLAS PARA CONVERSIONES (CRÍTICO):
SI es CONVERSIÓN, SIEMPRE llenar moneda_destino y monto_destino:

DETECCIÓN AUTOMÁTICA DE MONEDA ORIGEN:
1. Si el destino es "Bs" o menciona "bolivar" o "venezuela" → ORIGEN es USDT (Binance → Venezuela)
2. Si el destino es "USDT" o menciona "binance" o "cripto" → ORIGEN es USD (Ecuador → Binance)
3. Si el destino es "USD" → ORIGEN es USDT o Bs (dependiendo contexto)
4. Si NO especifica origen pero el monto destino es muy grande (>1000) y destino es Bs → ORIGEN es USDT
5. Si dice "por X mil" con "Bs" → ORIGEN es USDT
6. Si el monto origen es pequeño (<200) y destino es grande (>1000) → ORIGEN es USDT, destino es Bs

REGLA DE ORO PARA "cambié X por Y Bs":
- Si X < 500 y Y es en miles (ej: 10000, 5000, 3650) → USDT → Bs, ubicación origen: Binance

EJEMPLOS CRÍTICOS (SEGUIR EXACTAMENTE):
1. "cambié 33.06 por 10mil Bs" 
   → {{tipo: "Conversión", moneda: "USDT", monto: 33.06, moneda_destino: "Bs", monto_destino: 10000}}

2. "cambié 33,06 por 10mil bs" 
   → {{tipo: "Conversión", moneda: "USDT", monto: 33.06, moneda_destino: "Bs", monto_destino: 10000}}

3. "cambié 100 usdt por 3650 bs" 
   → {{tipo: "Conversión", moneda: "USDT", monto: 100, moneda_destino: "Bs", monto_destino: 3650}}

4. "cambié 125 usd por 120 usdt" 
   → {{tipo: "Conversión", moneda: "USD", monto: 125, moneda_destino: "USDT", monto_destino: 120}}

5. "cambié 125 por 120" 
   → {{tipo: "Conversión", moneda: "USD", monto: 125, moneda_destino: "USDT", monto_destino: 120}}

6. "cambié 100 por 3650 bs" 
   → {{tipo: "Conversión", moneda: "USDT", monto: 100, moneda_destino: "Bs", monto_destino: 3650}}

REGLAS PARA MONEDAS (UBICACIONES):
- Si menciona "bs" o "bolivar" → moneda="Bs", ubicacion="Venezuela"
- Si menciona "usdt" o "binance" → moneda="USDT", ubicacion="Binance"
- Si menciona "usd" → moneda="USD", ubicacion="Ecuador"
- Si NO especifica en CONVERSIÓN:
  - Si destino es Bs → origen es USDT (Binance)
  - Si destino es USDT → origen es USD (Ecuador)
  - Si monto_destino > 1000 y destino es Bs → origen es USDT
- Si NO especifica en EGRESO/INGRESO → asumir USD, ubicacion="Ecuador"

VALIDACIÓN:
✓ monto DEBE ser número positivo
✓ Si tipo="Conversión": moneda_destino y monto_destino NO DEBEN ser null o 0
✓ descripcion DEBE describir la transacción claramente
✓ NUNCA guardar conversión como "Conversión" en ubicacion, siempre especificar Ecuador/Binance/Venezuela
"""

        response = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": "Eres un asistente que clasifica transacciones financieras. Responde SOLO con JSON válido."},
                {"role": "user", "content": prompt}
            ],
            model="llama-3.3-70b-versatile",
            temperature=0.3,
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

        # Agregar campos opcionales si no existen
        if 'moneda_destino' not in result:
            result['moneda_destino'] = None
        if 'monto_destino' not in result:
            result['monto_destino'] = None

        # Validar conversión: si es Conversión, debe tener moneda_destino y monto_destino
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