from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "I am alive! (ChatBotFinanzas)"

import os
from waitress import serve
import logging

# Silenciar logs de waitress/werkzeug
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

def run():
  port = int(os.environ.get("PORT", 8080))
  # Usar waitress para producción
  serve(app, host='0.0.0.0', port=port, _quiet=True)

def keep_alive():
    t = Thread(target=run)
    t.start()
