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
import google.generativeai as genai

# Importar módulos locales
from tasas import GestorTasas
from saldos import GestorSaldos
from deudas import GestorDeudas
from prompts import SYSTEM_PROMPT
import keep_alive
import threading

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
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

groq_client = Groq(api_key=GROQ_API_KEY)

# Configurar Gemini si hay key disponible
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    logger.info("✅ Gemini API configurada (será usada para Vision)")
else:
    logger.warning("⚠️ GEMINI_API_KEY no encontrada, usando Groq Vision (menos preciso)")

gestor_tasas = GestorTasas()  # Instancia global
gestor_deudas = None # Se inicializa al conectar con Sheets

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
        # Siempre usar credenciales JSON (funciona en Local, Railway y Replit si se configura el secret)
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
            spreadsheet = gc.open("Finanzas Personales V2 - Bot")
        except gspread.SpreadsheetNotFound:
            logger.info("Hoja no encontrada. Intentando crear...")
            spreadsheet = gc.create("Finanzas Personales V2 - Bot")
            
            # Compartir inmediatamente (solo si el bot la creó)
            try:
                user_email = os.getenv('USER_EMAIL', 'prueba@prueba.com')
                if user_email:
                    spreadsheet.share(user_email, perm_type='user', role='writer')
                    logger.info(f"Hoja creada y compartida con {user_email}")
            except Exception as e:
                logger.error(f"Error al compartir hoja: {e}")

        # ✅ VERIFICAR Y CONFIGURAR ENCABEZADOS (Funciona para hoja nueva o creada manualmente)
        worksheet = spreadsheet.sheet1
        headers = ['Fecha', 'Tipo', 'Categoría', 'Ubicación', 'Moneda', 'Monto', 'Tasa Usada', 'USD Equivalente', 'Descripción']
        
        # Si A1 está vacío, asumimos que es nueva y ponemos encabezados
        if not worksheet.acell('A1').value:
            worksheet.update(range_name='A1:I1', values=[headers])
            logger.info("Encabezados inicializados")
        
        # Inicializar gestor de deudas
        global gestor_deudas
        gestor_deudas = GestorDeudas(spreadsheet)

        return spreadsheet
    except Exception as e:
        logger.error(f"Error al obtener/crear spreadsheet: {e}")
        raise

def classify_transaction(text: str) -> dict:
    """Usa Groq para clasificar la transacción con ubicación y moneda"""
    try:
        # Normalizar entrada
        normalized_text = normalize_input(text)

        # Preparar prompt
        prompt_content = SYSTEM_PROMPT.replace("{normalized_text}", normalized_text)

        response = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": "Responde siempre en JSON puro."},
                {"role": "user", "content": prompt_content}
            ],
            model="llama-3.3-70b-versatile",
            temperature=0.1,
            max_tokens=500
        )

        result_text = response.choices[0].message.content.strip()

        # Limpiar markdown si está presente
        if result_text.startswith('```'):
            result_text = result_text.split('```')[1]
            if result_text.strip().startswith('json'):
                result_text = result_text.strip()[4:]
            result_text = result_text.strip()

        logger.info(f"Raw JSON response: {result_text}")
        result = json.loads(result_text)

        required_keys = ['tipo', 'categoria', 'ubicacion', 'moneda', 'monto', 'descripcion']
        # Relaxed check: if key missing, try to fill defaults or warn
        for key in required_keys:
            if key not in result:
                if key == 'ubicacion': result['ubicacion'] = 'Venezuela'
                elif key == 'moneda': result['moneda'] = 'Bs'
                else:
                    raise ValueError(f"Falta campo requerido: {key}")

        # Validar monto
        try:
            monto = float(result['monto'])
            if monto <= 0:
                raise ValueError("El monto debe ser positivo")
            result['monto'] = monto
        except:
            raise ValueError(f"Monto inválido: {result.get('monto')}")

        # Campos opcionales de crédito
        result['es_credito'] = result.get('es_credito', False)
        result['monto_total_credito'] = result.get('monto_total_credito')
        result['es_pago_cuota'] = result.get('es_pago_cuota', False)
        result['referencia_deuda'] = result.get('referencia_deuda')

        # Campos opcionales de conversión
        result['moneda_destino'] = result.get('moneda_destino')
        result['monto_destino'] = result.get('monto_destino')

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
        
        # 🟢 LÓGICA DE CRÉDITOS Y DEUDAS
        es_credito = transaction_data.get('es_credito')
        es_pago_cuota = transaction_data.get('es_pago_cuota')
        
        msg_extra = ""
        
        if es_credito:
            total = transaction_data.get('monto_total_credito', 0)
            inicial = transaction_data.get('monto')
            desc = transaction_data.get('descripcion')
            
            # Crear deuda
            deuda_id, restante = gestor_deudas.crear_deuda(desc, total, inicial)
            msg_extra = f"\n📦 Deuda creada: Restan ${restante:.2f}"
            
        elif es_pago_cuota:
            referencia = transaction_data.get('referencia_deuda')
            monto_pago = transaction_data.get('monto')
            
            # Registrar pago
            success, info = gestor_deudas.registrar_pago_cuota(referencia, monto_pago)
            if success:
                msg_extra = f"\n💳 {info}"
            else:
                msg_extra = f"\n⚠️ Alerta Deuda: {info}"



        # 📅 FECHA: Usar la provista o la actual
        fecha_str = transaction_data.get('fecha')
        if fecha_str:
            # Intentar parsear fecha
            try:
                # Si viene "ayer"
                if fecha_str.lower() == 'ayer':
                    fecha_dt = datetime.now() - datetime.timedelta(days=1)
                elif fecha_str.lower() == 'hoy':
                     fecha_dt = datetime.now()
                else:
                    # Tratar de parsear DD/MM/YYYY o DD-MM-YYYY
                    fecha_limpia = fecha_str.replace('-', '/')
                    fecha_dt = datetime.strptime(fecha_limpia, "%d/%m/%Y")
                fecha = fecha_dt.strftime("%Y-%m-%d %H:%M:%S")
            except:
                # Fallback
                fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        else:
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
                # 🔄 TASA AUTOMÁTICA (Histórica o Actual)
                try:
                    fecha_dt = datetime.strptime(fecha, "%Y-%m-%d %H:%M:%S")
                    if fecha_dt.date() < datetime.now().date():
                        tasa_usada = gestor_tasas.obtener_tasa_historica(fecha_dt.strftime("%Y-%m-%d"))
                except:
                    pass
                
                # Si falló histórica o es hoy
                if not tasa_usada:
                    tasa_usada = gestor_tasas.obtener_tasa()
            if not tasa_usada:
                logger.error("No hay tasa para convertir Bs")
                return False, "Error tasa"
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

        # Usar table_range='A1' para evitar problemas con filtros
        worksheet.append_row(row, table_range="A1")
        logger.info(f"Transacción guardada: {row}")
        
        # Retornamos True y el mensaje extra
        return True, msg_extra

    except Exception as e:
        logger.error(f"Error al guardar en Sheets: {e}")
        return False, str(e)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Procesa fotos de facturas"""
    if not update.message or not update.message.photo:
        return

    await update.message.reply_text("📸 Analizando factura, dame unos segundos...")

    try:
        # 1. Obtener la foto de mayor resolución
        photo_file = await update.message.photo[-1].get_file()
        
        # 2. Descargar imagen en memoria
        from io import BytesIO
        img_buffer = BytesIO()
        await photo_file.download_to_memory(out=img_buffer)
        img_buffer.seek(0)
        
        # 3. Analizar con Gemini o Groq Vision
        prompt_vision = """Analiza esta imagen de factura/recibo.
        
        Responde SOLO con este JSON (extrae TODOS los campos que encuentres):
        {
            "tipo": "Egreso",
            "categoria": "Alimentación, Transporte, Salud, Servicios, Compras, Limpieza u Otro",
            "ubicacion": "Ecuador" o "Venezuela" (inferir por moneda: Bs=Venezuela, USD=Ecuador),
            "moneda": "USD" o "Bs",
            "subtotal": número o null (si aparece "Subtotal", "Base Imponible", o "BI"),
            "iva": número o null (si aparece "IVA", "Impuesto", o "Tax"),
            "total": número (el monto en la línea "TOTAL" o el número más grande al final),
            "descripcion": "nombre del local + items principales",
            "fecha": "DD/MM/YYYY" o null (fecha de emisión - verifica el AÑO, debe ser 2024 o 2025),
            "tasa_especifica": número o null
        }
        """
        
        result_text = None
        
        # 🔥 PRIORIDAD 1: Usar Gemini si está disponible
        if GEMINI_API_KEY:
            try:
                img_buffer.seek(0)
                from PIL import Image
                image = Image.open(img_buffer)
                
                # Usar el alias genérico "latest" que suele ser más estable en quota
                model_name = 'models/gemini-flash-latest' 
                model = genai.GenerativeModel(model_name)
                
                # Intentar hasta 2 veces si hay error de quota (429)
                import time
                from google.api_core import exceptions
                
                for attempt in range(2):
                    try:
                        response = model.generate_content([prompt_vision, image])
                        result_text = response.text.strip()
                        break # Éxito, salir del loop
                    except exceptions.ResourceExhausted:
                        if attempt == 0:
                            logger.warning("⚠️ Quota de Gemini excedida (429). Esperando 7s para reintentar...")
                            time.sleep(7)
                            continue
                        else:
                            raise # Falló segunda vez
                
                # Limpiar markdown
                if result_text and result_text.startswith('```'):
                    result_text = result_text.split('```')[1]
                    if result_text.strip().startswith('json'):
                        result_text = result_text.strip()[4:]
                    result_text = result_text.strip()
                
                logger.info(f"✅ Gemini Vision JSON: {result_text}")
                
            except Exception as e:
                logger.error(f"Error con Gemini Vision: {e}, fallback a Groq...")
                result_text = None
        
        # 🔄 FALLBACK: Usar Groq Vision si Gemini falló o no está disponible
        if not result_text:
            img_buffer.seek(0)
            base64_image = base64.b64encode(img_buffer.read()).decode('utf-8')
            
            response = groq_client.chat.completions.create(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt_vision},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{base64_image}",
                                },
                            },
                        ],
                    }
                ],
                model=os.getenv('GROQ_VISION_MODEL', 'meta-llama/llama-4-scout-17b-16e-instruct'),
                temperature=0.1,
                max_tokens=500,
            )

            result_text = response.choices[0].message.content.strip()
            
            # Limpiar markdown
            if result_text.startswith('```'):
                result_text = result_text.split('```')[1]
                if result_text.strip().startswith('json'):
                    result_text = result_text.strip()[4:]
                result_text = result_text.strip()
            
            logger.info(f"Vision JSON (Groq): {result_text}")
        logger.info(f"Vision JSON: {result_text}")
        transaction = json.loads(result_text)
        
        # 🧮 VALIDAR Y CORREGIR EL MONTO TOTAL
        subtotal = transaction.get('subtotal')
        iva = transaction.get('iva')
        total = transaction.get('total')
        
        # Si hay subtotal + iva, calcular el total real
        if subtotal and iva:
            calculated_total = subtotal + iva
            # Si el "total" reportado difiere, usar el calculado
            if not total or abs(total - calculated_total) > 0.5:
                logger.warning(f"Total corregido: {total} -> {calculated_total} (Subtotal: {subtotal}, IVA: {iva})")
                total = calculated_total
        
        # Asignar el monto final
        transaction['monto'] = total if total else transaction.get('total', 0)
        
        # 📅 VALIDAR AÑO (2023 -> 2025 si es sospechoso)
        fecha_str = transaction.get('fecha')
        if fecha_str and '2023' in fecha_str:
            logger.warning(f"Fecha sospechosa (2023), corrigiendo a 2025: {fecha_str}")
            fecha_str = fecha_str.replace('2023', '2025')
            transaction['fecha'] = fecha_str
        
        # 🐛 DEBUG MODE: Mostrar lo que vio el modelo
        await update.message.reply_text(f"🤖 Debug Vision:\n{json.dumps(transaction, indent=2)}")
        
        # Validar
        if not transaction.get('monto') or not transaction.get('moneda'):
            await update.message.reply_text("❌ No pude leer bien el monto o la moneda de la foto. Intenta con texto.")
            return

        # 5. Guardar
        # Obtener tasa si es Bs
        tasa_para_guardar = None
        if transaction['moneda'] == "Bs":
            tasa_para_guardar = transaction.get('tasa_especifica')

        success, msg_extra = save_to_sheets(transaction, tasa_para_guardar)
        
        if success:
            # Calcular USD equivalente
            if transaction['moneda'] == "Bs" and tasa_para_guardar:
                monto_usd = transaction['monto'] / tasa_para_guardar
            else:
                monto_usd = transaction['monto']
                
            confirmation = f"""📸 ¡Factura Procesada!

📍 Ubicación: {transaction['ubicacion']}
🏷️ Categoría: {transaction['categoria']}
💱 Moneda: {transaction['moneda']}
💵 Monto: {transaction['monto']:.2f}
📝 Desc: {transaction['descripcion']}

✅ Guardado en Google Sheets"""
            await update.message.reply_text(confirmation)
        else:
             await update.message.reply_text(f"❌ Error al guardar: {msg_extra}")

    except Exception as e:
        logger.error(f"Error procesando foto: {e}")
        await update.message.reply_text(f"❌ Error analizando la imagen: {str(e)}\nIntenta de nuevo o envía el texto.")


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
/resumen - Resumen del mes actual (ingresos, egresos, balance)

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
/resumen - Resumen del mes actual (ingresos, egresos, balance)

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

async def comando_deudas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /deudas - Ver resumen de deudas"""
    try:
        if not gestor_deudas:
            await update.message.reply_text("❌ Error: Gestor de deudas no inicializado")
            return
            
        resumen = gestor_deudas.obtener_resumen()
        await update.message.reply_text(resumen)
        
    except Exception as e:
        logger.error(f"Error en comando /deudas: {e}")
        await update.message.reply_text(f"❌ Error interno: {e}")

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

            # Guardar ambas transacciones (Unpack tuple)
            success_egreso, _ = save_to_sheets(egreso_data)
            success_ingreso, _ = save_to_sheets(ingreso_data)

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
            tasa_para_guardar = transaction.get('tasa_especifica')

        success, msg_extra = save_to_sheets(transaction, tasa_para_guardar)

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
{msg_extra}

✅ Guardado en Google Sheets"""

            await update.message.reply_text(confirmation)
        else:
            error_msg = msg_extra if msg_extra else "Intenta de nuevo."
            await update.message.reply_text(f"❌ Error al guardar: {error_msg}")

    except Exception as e:
        logger.error(f"Error procesando mensaje: {e}")
        await update.message.reply_text(
            "❌ No pude procesar tu mensaje. Intenta con:\n\n"
            "GASTOS/INGRESOS:\n"
            "• 'gasto 50 bs comida' (o sin acento: 'gaste')\n"
            "• 'ingreso 2000 sueldo'\n"
            "• 'gasté 15 usd transporte' (o: 'gaste')\n"
            "• 'cobré 500 freelance' (o: 'cobre')\n\n"
            "CRÉDITO:\n"
            "• 'compre telefono 200 pagando 50 inicial'\n"
            "• 'pago cuota telefono 25'\n\n"
            "CONVERSIONES:\n"
            "• 'cambié 125 por 120' (USD→USDT, o: 'cambie')\n"
            "• 'cambié 126.5 por 125' (o: 'cambie')\n"
            "• 'cambié 120 por 2760 bs' (o: 'cambie')\n"
            "• 'cambié 102.24 usd a 100 usdt' (o: 'cambie')"
        )
        
async def comando_resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /resumen - Ver resumen del mes actual"""
    try:
        from datetime import datetime
        
        spreadsheet = get_or_create_spreadsheet()
        gestor_saldos = GestorSaldos(spreadsheet.sheet1, gestor_tasas)

        # Obtener todas las transacciones
        transacciones = gestor_saldos.obtener_todas_transacciones()
        
        # Filtrar transacciones del mes actual
        mes_actual = datetime.now().month
        año_actual = datetime.now().year
        
        gastos_por_categoria = {}
        ingresos_por_categoria = {}
        total_ingresos = 0
        total_egresos = 0
        
        for trans in transacciones:
            try:
                fecha_str = trans.get("Fecha", "")
                fecha = datetime.strptime(fecha_str[:10], "%Y-%m-%d")
                
                # Solo transacciones del mes actual
                if fecha.month != mes_actual or fecha.year != año_actual:
                    continue
                
                tipo = trans.get("Tipo", "").lower()
                categoria = trans.get("Categoría", "Otros")
                monto_str = str(trans.get("USD Equivalente", 0)).replace(',', '.')
                
                try:
                    monto = abs(float(monto_str))
                except:
                    monto = 0
                
                if tipo == "egreso":
                    if categoria not in gastos_por_categoria:
                        gastos_por_categoria[categoria] = 0
                    gastos_por_categoria[categoria] += monto
                    total_egresos += monto
                    
                elif tipo == "ingreso":
                    if categoria not in ingresos_por_categoria:
                        ingresos_por_categoria[categoria] = 0
                    ingresos_por_categoria[categoria] += monto
                    total_ingresos += monto
                    
            except Exception as e:
                logger.warning(f"Error procesando transacción para resumen: {e}")
                continue
        
        # Construir mensaje
        mes_nombre = datetime.now().strftime("%B").upper()
        
        mensaje = f"📊 RESUMEN DEL MES - {mes_nombre} {año_actual}\n"
        mensaje += "═══════════════════════════════════════════\n\n"
        
        # INGRESOS
        mensaje += "💰 INGRESOS\n"
        mensaje += "───────────────────────────────────────\n"
        if ingresos_por_categoria:
            for cat in sorted(ingresos_por_categoria.keys()):
                monto = ingresos_por_categoria[cat]
                mensaje += f"  {cat}: ${monto:.2f}\n"
            mensaje += f"\n  TOTAL INGRESOS: ${total_ingresos:.2f}\n"
        else:
            mensaje += "  Sin ingresos registrados\n"
        
        # EGRESOS
        mensaje += "\n💸 EGRESOS\n"
        mensaje += "───────────────────────────────────────\n"
        if gastos_por_categoria:
            for cat in sorted(gastos_por_categoria.keys()):
                monto = gastos_por_categoria[cat]
                mensaje += f"  {cat}: ${monto:.2f}\n"
            mensaje += f"\n  TOTAL EGRESOS: ${total_egresos:.2f}\n"
        else:
            mensaje += "  Sin egresos registrados\n"
        
        # RESUMEN FINAL
        balance = total_ingresos - total_egresos
        mensaje += "\n═══════════════════════════════════════════\n"
        mensaje += f"📈 BALANCE: ${balance:.2f}\n"
        
        if balance > 0:
            mensaje += "✅ Mes superavitario\n"
        elif balance < 0:
            mensaje += "⚠️ Mes deficitario\n"
        else:
            mensaje += "⚖️ Mes balanceado\n"
        
        await update.message.reply_text(mensaje)
        
    except Exception as e:
        logger.error(f"Error en comando /resumen: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

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

    # Inicializar Spreadsheet y Gestores al inicio
    try:
        get_or_create_spreadsheet()
        logger.info("✅ Google Sheets conectado y gestores inicializados.")
    except Exception as e:
        logger.error(f"❌ Error fatal iniciando Google Sheets: {e}")
        return

    # Iniciar servidor web para Replit
    keep_alive.keep_alive()

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Comandos
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("tasa", comando_tasa))
    application.add_handler(CommandHandler("settasa", comando_settasa))
    application.add_handler(CommandHandler("saldo", comando_saldo))
    application.add_handler(CommandHandler("resumen", comando_resumen))
    application.add_handler(CommandHandler("deudas", comando_deudas))

    # Mensajes
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo)) # Nuevo handler para fotos
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Error handler
    application.add_error_handler(error_handler)

    logger.info("Bot iniciado. Esperando mensajes...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()