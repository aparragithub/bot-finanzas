import os
from groq import Groq

# Intentar obtener key de env o de archivo .env simulado
api_key = os.getenv("GROQ_API_KEY")

try:
    if not api_key:
        with open(".env", "r") as f:
            for line in f:
                if line.startswith("GROQ_API_KEY"):
                    api_key = line.split("=")[1].strip().strip('"').strip("'")
                    break
except:
    pass

if not api_key:
    print("❌ Error: No se encontró GROQ_API_KEY. Configura el Secret en Replit.")
    exit(1)

print(f"🔑 Usando Key: {api_key[:5]}...{api_key[-3:]}")

try:
    client = Groq(api_key=api_key)
    print("📡 Consultando API de Groq...")
    
    models = client.models.list()
    
    print("\n✅ MODELOS DISPONIBLES (Vision & Llama):")
    print("-" * 40)
    found_vision = False
    for m in models.data:
        mid = m.id
        if "vision" in mid or "llama" in mid:
            print(f"• {mid}")
            if "vision" in mid:
                found_vision = True
    print("-" * 40)
    
    if not found_vision:
        print("⚠️ ALERTA: No se encontraron modelos explícitos de 'vision'.")
        print("Es posible que Groq haya cambiado los nombres o restricto el acceso.")

except Exception as e:
    print(f"\n❌ Error fatal consultando API: {e}")
