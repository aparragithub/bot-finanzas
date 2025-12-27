
import os
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv('GEMINI_API_KEY')
if not api_key:
    print("❌ No GEMINI_API_KEY found in .env")
    exit()

genai.configure(api_key=api_key)

print(f"🔑 Testing Key: {api_key[:5]}...")

try:
    print("📡 Listing available models...")
    for m in genai.list_models():
        if 'generateContent' in m.supported_generation_methods:
            print(f"✅ Found: {m.name}")
except Exception as e:
    print(f"❌ Error listing models: {e}")
