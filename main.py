import os
import logging
from datetime import datetime, timedelta
import base64
import json
import requests
import google.generativeai as genai
import re

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

# Importar módulos locales
from tasas import GestorTasas
from saldos import GestorSaldos
from deudas import GestorDeudas
# from cuentas import GestorCuentas  <-- REMOVIDO
from prompts import SYSTEM_PROMPT
import keep_alive

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
gestor_saldos = None # Se inicializa al conectar

def normalize_input(text: str) -> str:
    """Normaliza el input para mejorar compatibilidad sin acentos"""
    normalized = text.lower()
    replacements = {
        'cambie ': 'cambié ', 'cambie,': 'cambié,', 'cambie.': 'cambié.',
        'gaste ': 'gasté ', 'gaste,': 'gasté,', 'gaste.': 'gasté.',
        'cobre ': 'cobré ', 'cobre,': 'cobré,', 'cobre.': 'cobré.',
        'compre ': 'compré ', 'compre,': 'compré,', 'compre.': 'compré.',
        'pague ': 'pagué ', 'pague,': 'pagué,', 'pague.': 'pagué.',
    }
    for key, value in replacements.items():
        normalized = normalized.replace(key, value)
    return normalized

def get_google_sheets_client():
    """Obtiene el cliente de Google Sheets"""
    try:
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
            try:
                user_email = os.getenv('USER_EMAIL', 'prueba@prueba.com')
                if user_email:
                    spreadsheet.share(user_email, perm_type='user', role='writer')
            except Exception as e:
                logger.error(f"Error al compartir hoja: {e}")

        # Configurar hoja principal
        worksheet = spreadsheet.sheet1
        headers = ['Fecha', 'Tipo', 'Categoría', 'Ubicación', 'Moneda', 'Monto', 'Tasa Usada', 'USD Equivalente', 'Descripción']
        if not worksheet.acell('A1').value:
            worksheet.update(range_name='A1:I1', values=[headers])
            logger.info("Encabezados inicializados")
        
        # Inicializar gestores
        global gestor_deudas, gestor_saldos
        gestor_deudas = GestorDeudas(spreadsheet)
        gestor_saldos = GestorSaldos(worksheet, gestor_tasas) # Usamos la versión de saldos.py

        return spreadsheet
    except Exception as e:
        logger.error(f"Error al obtener/crear spreadsheet: {e}")
        raise

def classify_transaction(text: str) -> dict:
    """Usa Groq para clasificar la transacción con ubicación y moneda"""
    try:
        normalized_text = normalize_input(text)
        
        # ⚠️ Detectar Comandos Cashea Naturales antes de llamar a la IA
        if "cashea" in normalized_text and "gasto" in normalized_text:
            return {
                "tipo": "Egreso",
                "categoria": "Compras",
                "ubicacion": "Venezuela",
                "moneda": "USD", # Default cashea
                "monto": 0, # Se calculará después
                "descripcion": normalized_text,
                "es_cashea": True, # Flag especial
                "raw_text": normalized_text
            }

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
        if result_text.startswith('```'):
            result_text = result_text.split('```')[1]
            if result_text.strip().startswith('json'):
                result_text = result_text.strip()[4:]
            result_text = result_text.strip()

        result = json.loads(result_text)
        
        # Validación básica y correcciones
        required_keys = ['tipo', 'categoria', 'ubicacion', 'moneda', 'monto', 'descripcion']
        for key in required_keys:
            if key not in result:
                if key == 'ubicacion': result['ubicacion'] = 'Venezuela'
                elif key == 'moneda': result['moneda'] = 'Bs'
                else: raise ValueError(f"Falta campo requerido: {key}")

        try:
            result['monto'] = float(result['monto'])
        except:
             result['monto'] = 0

        return result

    except Exception as e:
        logger.error(f"Error en clasificación: {e}")
        raise

def save_to_sheets(transaction_data: dict, tasa_usada: float = None) -> bool:
    """Guarda la transacción en Google Sheets"""
    try:
        spreadsheet = get_or_create_spreadsheet()
        worksheet = spreadsheet.sheet1
        
        msg_extra = ""
        fecha_compra = datetime.now().strftime("%Y-%m-%d")

        # 🟢 LÓGICA CASHEA (V3)
        if transaction_data.get('es_cashea'):
            texto = transaction_data.get('raw_text', '')
            
            numeros = re.findall(r'\d+\.?\d*', texto)
            if not numeros: return False, "No encontré el monto de la compra"
            monto_total = float(numeros[0])
            
            linea = "cotidiana" if "cotidiana" in texto else "principal"
            
            match_inicial = re.search(r'inicial\s+(\d+)', texto)
            if match_inicial:
                inicial_usuario = float(match_inicial.group(1))
                simulacion = gestor_deudas.simular_compra_cashea(monto_total, linea)
                if simulacion and simulacion['es_ajustado'] and inicial_usuario < simulacion['inicial_a_pagar']:
                    msg_extra = f"\n⚠️ OJO: Tu inicial manual (${inicial_usuario}) es menor a la requerida por límite (${simulacion['inicial_a_pagar']:.2f})."
                monto_inicial_real = inicial_usuario
            else:
                simulacion = gestor_deudas.simular_compra_cashea(monto_total, linea)
                if not simulacion: return False, "Error simulando crédito"
                monto_inicial_real = simulacion['inicial_a_pagar']
                if simulacion['es_ajustado']:
                    msg_extra = f"\n⚠️ Inicial Ajustada Automáticamente: ${monto_inicial_real:.2f}"

            desc = f"Cashea: {transaction_data.get('descripcion', 'Compra')}"
            gestor_deudas.crear_deuda(desc, monto_total, monto_inicial_real, tipo=f"Cashea ({linea})")
            
            transaction_data['monto'] = monto_inicial_real
            transaction_data['descripcion'] = f"{desc} (Inicial)"
            msg_extra += f"\n📦 Deuda Cashea creada. Resta: ${monto_total - monto_inicial_real:.2f}"

        elif transaction_data.get('es_credito'):
             gestor_deudas.crear_deuda(transaction_data['descripcion'], transaction_data['monto_total_credito'], transaction_data['monto'])

        elif transaction_data.get('es_pago_cuota'):
            success, info = gestor_deudas.registrar_pago_cuota(transaction_data.get('referencia_deuda'), transaction_data['monto'])
            msg_extra = f"\n💳 {info}" if success else f"\n⚠️ {info}"

        # 📅 FECHA
        fecha_str = transaction_data.get('fecha')
        fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if fecha_str:
            try:
                if fecha_str.lower() == 'ayer': fecha_dt = datetime.now() - timedelta(days=1)
                elif fecha_str.lower() == 'hoy': fecha_dt = datetime.now()
                else: fecha_dt = datetime.strptime(fecha_str.replace('-', '/'), "%d/%m/%Y")
                fecha = fecha_dt.strftime("%Y-%m-%d %H:%M:%S")
            except: pass

        # Convertir a USD / Bs logic
        monto_original = transaction_data['monto']
        moneda = transaction_data['moneda']
        tipo = transaction_data['tipo'].lower()

        if tipo == "egreso":
            monto_original = -abs(monto_original)
            monto_usd_multiplicador = -1
        else:
            monto_usd_multiplicador = 1

        tasa_usada_final = 1.0
        monto_usd = 0

        if moneda == "Bs":
            if not tasa_usada:
                try:
                    fecha_dt = datetime.strptime(fecha, "%Y-%m-%d %H:%M:%S")
                    if fecha_dt.date() < datetime.now().date():
                        tasa_usada = gestor_tasas.obtener_tasa_historica(fecha_dt.strftime("%Y-%m-%d"))
                except: pass
                if not tasa_usada: tasa_usada = gestor_tasas.obtener_tasa()
            
            tasa_usada_final = tasa_usada if tasa_usada else 0
            if tasa_usada_final > 0:
                monto_usd = (monto_original / tasa_usada_final) * monto_usd_multiplicador
        
        elif moneda in ["USD", "USDT"]:
             monto_usd = monto_original
        
        row = [
            fecha, transaction_data['tipo'], transaction_data['categoria'],
            transaction_data['ubicacion'], moneda, monto_original,
            tasa_usada_final if moneda == "Bs" else "", monto_usd,
            transaction_data['descripcion']
        ]

        worksheet.append_row(row, table_range="A1")
        return True, msg_extra

    except Exception as e:
        logger.error(f"Error guardar Sheets: {e}")
        return False, str(e)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Procesa fotos de facturas (Integración Gemini)"""
    if not update.message or not update.message.photo: return
    await update.message.reply_text("📸 Analizando factura...")

    try:
        photo_file = await update.message.photo[-1].get_file()
        from io import BytesIO
        img_buffer = BytesIO()
        await photo_file.download_to_memory(out=img_buffer)
        
        prompt_vision = """Analiza esta imagen. Responde SOLO JSON:
        {
            "tipo": "Egreso",
            "categoria": "Alimentación, Transporte, Salud, Servicios, Compras, Limpieza u Otro",
            "ubicacion": "Ecuador" o "Venezuela" (inferir por moneda: Bs=Venezuela, USD=Ecuador),
            "moneda": "USD" o "Bs",
            "subtotal": número o null,
            "iva": número o null,
            "total": número,
            "descripcion": "nombre del local + items",
            "fecha": "DD/MM/YYYY" o null (verifica año 2025),
            "tasa_especifica": número o null
        }"""
        
        # Lógica simplificada Gemini
        img_buffer.seek(0)
        from PIL import Image
        image = Image.open(img_buffer)
        
        model = genai.GenerativeModel('models/gemini-flash-latest')
        response = model.generate_content([prompt_vision, image])
        result_text = response.text.strip()
        
        if result_text.startswith('```'): 
            result_text = result_text.split('```')[1].replace('json','').strip()
            
        transaction = json.loads(result_text)
        
        # Mapear 'total' a 'monto' si es necesario
        if 'total' in transaction and 'monto' not in transaction:
            transaction['monto'] = transaction['total']
             
        tasa = transaction.get('tasa_especifica') if transaction['moneda'] == 'Bs' else None
        
        success, msg = save_to_sheets(transaction, tasa)
        if success:
            await update.message.reply_text(f"✅ Factura Guardada!\n💵 Total: {transaction['moneda']} {transaction.get('monto')}\n📝 {transaction.get('descripcion')}")
        else:
            await update.message.reply_text(f"❌ Error: {msg}")

    except Exception as e:
        logger.error(f"Error Vision: {e}")
        await update.message.reply_text("❌ Error analizando imagen.")

# --- COMANDOS ESTRUCTURALES ---

async def comando_cashea(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Simulador de Compra Cashea"""
    try:
        args = context.args
        if not args or len(args) < 1:
            await update.message.reply_text("🔎 Uso: `/cashea [monto] [linea:p/c]`\nEj: `/cashea 120`")
            return
            
        monto = float(args[0])
        linea = args[1] if len(args) > 1 else "principal"
        
        if not gestor_deudas: get_or_create_spreadsheet()
        
        res = gestor_deudas.simular_compra_cashea(monto, linea)
        
        msg = f"🛍️ **SIMULACIÓN CASHEA (${monto:.2f})**\n\n"
        msg += f"• **Inicial:** `${res['inicial_a_pagar']:.2f}`\n"
        msg += f"• **Crédito:** `${res['monto_financiar']:.2f}`\n"
        msg += f"• **Disponible Antes:** `${res['disponible_antes']:.2f}`\n\n"
        msg += f"{res['mensaje']}"
        
        await update.message.reply_text(msg, parse_mode="Markdown")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def comando_importardeuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Importa deuda con parsing inteligente.
    Soporta: /importardeuda [Texto libre con monto, cuotas y fecha]
    Ej: /importardeuda 56 usd 1 cuota Monitor 30/12/2025
    """
    try:
        args = context.args
        if not args:
            await update.message.reply_text("❌ Uso: `/importardeuda [Monto] [Cuotas opcional] [Desc] [Fecha opcional]`")
            return
            
        full_text = " ".join(args)
        
        # 1. Extraer FECHA (DD/MM/YYYY o hoy/ayer)
        fecha_match = re.search(r'\b(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})\b', full_text)
        prox_venc = datetime.now().strftime("%Y-%m-%d") # Default hoy
        
        if fecha_match:
            fecha_str = fecha_match.group(1).replace('-', '/')
            try:
                dt = datetime.strptime(fecha_str, "%d/%m/%Y")
                prox_venc = dt.strftime("%Y-%m-%d")
                full_text = full_text.replace(fecha_match.group(0), "") # Remover fecha
            except: pass
        elif "hoy" in full_text.lower():
            full_text = re.sub(r'\bhoy\b', '', full_text, flags=re.IGNORECASE)
        elif "ayer" in full_text.lower():
            prox_venc = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            full_text = re.sub(r'\bayer\b', '', full_text, flags=re.IGNORECASE)

        # 2. Extraer CUOTAS (N cuotas o simplemente segundo número)
        num_cuotas = 1
        # Buscar patrón explícito "X cuotas"
        cuotas_match = re.search(r'\b(\d+)\s*(?:cuota|plazo|mes|pago)s?\b', full_text, re.IGNORECASE)
        if cuotas_match:
             num_cuotas = int(cuotas_match.group(1))
             full_text = full_text.replace(cuotas_match.group(0), "")
        else:
             # Si no hay "cuotas", buscar si hay DOS números separados. asumimos 1ro=Monto, 2do=Cuotas
             numeros = re.findall(r'\b\d+(?:\.\d+)?\b', full_text)
             if len(numeros) >= 2:
                 # Si el segundo número es entero pequeño (<24), asumimos que son cuotas
                 posible_cuota = float(numeros[1])
                 if posible_cuota.is_integer() and posible_cuota < 24:
                     num_cuotas = int(posible_cuota)
                     # Remover solo la primera ocurrencia de ese número para no borrar el precio si son iguales
                     full_text = re.sub(r'\b' + str(int(posible_cuota)) + r'\b', '', full_text, count=1)

        # 3. Extraer MONTO (Primer número que quede)
        monto_match = re.search(r'\b\d+(?:\.\d+)?\b', full_text)
        if not monto_match:
            await update.message.reply_text("❌ No encontré el monto de la deuda.")
            return
            
        monto_cuota = float(monto_match.group(0))
        full_text = full_text.replace(monto_match.group(0), "", 1)
        
        # 4. Limpieza Final (Descripción)
        # Remover palabras basura
        basura = ['usd', 'bs', 'pesos', 'dolares', 'bolivares', '$', '€']
        for b in basura:
            full_text = re.sub(r'\b' + re.escape(b) + r'\b', '', full_text, flags=re.IGNORECASE)
            
        descripcion = re.sub(r'\s+', ' ', full_text).strip()
        if not descripcion: descripcion = "Deuda Importada"
        
        # Calcular Totales
        monto_total_deuda = monto_cuota * num_cuotas
        linea = "Cotidiana" if num_cuotas == 1 else "Principal"
        
        if not gestor_deudas: get_or_create_spreadsheet()
        
        gestor_deudas.crear_deuda(
            descripcion=f"Imp: {descripcion}", 
            monto_total=monto_total_deuda, 
            monto_inicial=0,
            tipo=f"Cashea ({linea}) - Importado",
            proximo_vencimiento=prox_venc
        )
        
        msg = f"✅ **Deuda Importada**\n"
        msg += f"📦 {descripcion}\n"
        msg += f"🔢 {num_cuotas} cuotas de ${monto_cuota}\n"
        msg += f"📅 Vence: {prox_venc}"
        
        await update.message.reply_text(msg, parse_mode="Markdown")

    except Exception as e:
        await update.message.reply_text(f"❌ Error interno: {e}")



async def comando_custodia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Registra fondos de terceros (Pasivo).
    Uso: /custodia [monto] [descripcion]
    Ej: /custodia 100 Ahorros Papa
    """
    try:
        args = context.args
        if not args or len(args) < 2:
            await update.message.reply_text("❌ Uso: `/custodia [monto] [descripcion]`\nEj: `/custodia 100 Ahorro Papa`")
            return
            
        monto = float(args[0])
        descripcion = " ".join(args[1:])
        
        if not gestor_deudas: get_or_create_spreadsheet()
        
        # Crear Pasivo tipo 'Custodia'
        gestor_deudas.crear_deuda(
            descripcion=f"Custodia: {descripcion}",
            monto_total=monto,
            monto_inicial=0,
            tipo="Custodia (Pasivo)",
            proximo_vencimiento="N/A" # No vence, es indeterminado
        )
        
        msg = f"🔐 **Fondo en Custodia Registrado**\n"
        msg += f"📝 Concepto: {descripcion}\n"
        msg += f"💰 Monto: ${monto}\n"
        msg += "⚠️ Recuerda registrar el INGRESO real si el dinero entró a tus cuentas (ej: `ingreso 100 usd binance`)."
        
        await update.message.reply_text(msg, parse_mode="Markdown")

    except ValueError:
        await update.message.reply_text("❌ El monto debe ser numérico")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def comando_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ver saldo acumulado (suma de transacciones)"""
    try:
        if not gestor_saldos: get_or_create_spreadsheet()
        
        # Si pasan argumentos, filtrar ubicación
        if context.args:
            ubicacion = context.args[0]
            mensaje = gestor_saldos.obtener_saldo_por_ubicacion_formateado(ubicacion)
        else:
            mensaje = gestor_saldos.obtener_portafolio_detallado()
            
        await update.message.reply_text(mensaje)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_msg = """👋 **Bienvenido a tu Bot Financiero V3** 🚀

Aquí tienes tu "Chuleta" de comandos rápidos:

📝 **GASTOS E INGRESOS (Básico)**
• `gasto 50 bs comida` (Gastos del día a día)
• `ingreso 2000 sueldo` (Tus entradas)
• `gasté 15 usd uber` (Reconoce monedas)

🛍️ **MODO CASHEA (V3)**
• **Nueva Compra:** `gasto 120 zapatos cashea`
  *(El bot calcula tu inicial y crea las cuotas automáticamente)*
• **Importar Deuda Vieja:** `/importardeuda 20 3 "TV" 15/01/2025`
  *(Para registrar lo que ya debes: 3 cuotas de $20)*

🏦 **CONTROL DE SALDOS**
• **Cargar Saldo Inicial:** `ingreso 500 bs banesco saldo inicial`
• **Ver mis Cuentas:** `/saldo`
• **Dinero de Terceros (Papá):** `/custodia 100 Ahorros Papa`
  *(Registra que 100 de tu saldo son prestados/custodia)*

💱 **CONVERSIONES (Binance)**
• `cambié 100 usd a 98 usdt`
• `cambié 50 usdt a 2500 bs`

📸 **FACTURAS**
¡Solo envíame una foto! Yo leo los montos y la fecha.

💡 **COMANDOS ÚTILES**
/saldo - Resumen total de tu dinero
/deudas - Ver tus créditos pendientes
/tasa - Ver precio del dólar BCV
"""
    await update.message.reply_text(help_msg, parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    text = update.message.text
    
    if "cashea" in text.lower() and "gasto" in text.lower():
        t_data = classify_transaction(text)
        success, msg = save_to_sheets(t_data)
        if success: await update.message.reply_text(f"🛍️ **Cashea Registrado!**\n{msg}")
        else: await update.message.reply_text(f"❌ Error: {msg}")
        return

    await update.message.reply_text("🔄 Procesando...")
    try:
        t_data = classify_transaction(text)
        success, msg = save_to_sheets(t_data)
        await update.message.reply_text("✅ Listo!" + msg if success else "❌ Error: " + str(msg))
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text("❌ No entendí.")


async def comando_simple_tasa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Tasa: {gestor_tasas.obtener_tasa()}")

async def comando_simple_deudas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(gestor_deudas.obtener_resumen())

def main():
    if not TELEGRAM_TOKEN: return
    try: get_or_create_spreadsheet()
    except: pass
    keep_alive.keep_alive()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cashea", comando_cashea))
    app.add_handler(CommandHandler("importardeuda", comando_importardeuda))
    app.add_handler(CommandHandler("custodia", comando_custodia))
    app.add_handler(CommandHandler("saldo", comando_saldo))
    app.add_handler(CommandHandler("tasa", comando_simple_tasa))
    app.add_handler(CommandHandler("deudas", comando_simple_deudas))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == '__main__':
    main()