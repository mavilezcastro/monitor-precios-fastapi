from fastapi import FastAPI,HTTPException,status
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
import os
from dotenv import load_dotenv
from pymongo import MongoClient
from datetime import datetime
import re
import json 
import urllib.request



# Interruptor
load_dotenv()

# Url
MONGO_URI = os.getenv("MONGO_URI")

# Cliente
cliente = MongoClient(MONGO_URI)

# Base de datos
db = cliente["montior_precios"]

# Registro de productos
coleccion_productos = db["productos"]


# Iniciar server con uvicorn main:app --reload
app = FastAPI(title="Monitor de Precios Inteligente",
    description="API para hacer web scraping de productos y seguir su historial de precios.")

# --- FUNCIONES AUXILIARES (UTILIDADES) ---
def limpiar_precio(texto_precio: str) -> float:
    """Extrae únicamente los números y decimales de un string de precio."""
    # Busca números y puntos/comas decimales (ej: "£51.77" -> "51.77")
    coincidencia = re.search(r"[-+]?\d*\.\d+|\d+", texto_precio)
    if coincidencia:
        return float(coincidencia.group())
    return 0.0

def enviar_alerta_discord(producto_titulo: str, precio_viejo: float, precio_nuevo: float, url: str):
    """
    Envía una notificación push con un diseño elegante (Embed) a un canal de Discord
    utilizando el webhook configurado en las variables de entorno..
    """
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")

    # Control de seguridad: Si no hay URL configurada, evitamos que el código falle
    if not webhook_url:
        print("⚠️ [ADVERTENCIA] No se encontró DISCORD_WEBHOOK_URL en el archivo .env")
        return
    
    # Calculamos el porcentaje de descuento para hacerlo más vistoso
    descuento = round(((precio_viejo - precio_nuevo) / precio_viejo) * 100, 1)
    # Creamos un diseño tipo 'Embed' (tarjeta elegante en Discord)
    payload = {
        "username": "Monitor de Precios Bot",
        "avatar_url": "https://i.imgur.com/vHdf7n3.png", # Icono de robot de ejemplo
        "embeds": [
            {
                "title": f"📉 ¡ALERTA DE OFERTA! - {descuento}% DE DESCUENTO",
                "description": f"El producto **{producto_titulo}** ha bajado de precio de forma drástica.",
                "url": url,
                "color": 3066993, # Color verde en formato decimal de Discord
                "fields": [
                    {
                        "name": "💰 Precio Anterior",
                        "value": f"£{precio_viejo}",
                        "inline": True
                    },
                    {
                        "name": "✨ Precio Nuevo",
                        "value": f"£{precio_nuevo}",
                        "inline": True
                    }
                ],
                "footer": {
                    "text": "Monitoreo Automatizado de Precios",
                },
                "timestamp": datetime.now().isoformat()
            }
        ]
    }

    try:
        # Convertimos el diccionario de Python a un string JSON y luego a bytes
        data_codificada = json.dumps(payload).encode('utf-8')
        
        # Preparamos la petición HTTP POST simulando ser un navegador (User-Agent)
        req = urllib.request.Request(
            webhook_url, 
            data=data_codificada, 
            headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'}
        )
        
        # Enviamos la petición
        with urllib.request.urlopen(req) as response:
            if response.status in [200, 204]:
                print(f"🚀 [WEBHOOK] Alerta enviada con éxito a Discord para: {producto_titulo}")
            else:
                print(f"⚠️ [WEBHOOK] Discord respondió con código: {response.status}")
                
    except Exception as e:
        print(f"❌ [WEBHOOK] Falló el envío al webhook de Discord: {str(e)}")

# --- MODELOS DE DATOS (PYDANTIC) ---
class ProductoRequest(BaseModel):
    url : str
    selector : str

# --- ENDPOINTS ---
@app.post("/check-price", status_code=status.HTTP_201_CREATED)
def check_price(request: ProductoRequest):
    # 1. Scraping del precio con Playwright
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(request.url, timeout=60000)
            
            # Extraemos el texto crudo de la página
            precio_crudo = page.locator(request.selector).first.inner_text()
            browser.close()
    except Exception as e:
        raise HTTPException(
            status_code=500, 
            detail=f"Error al hacer scraping de la página: {str(e)}"
        )

    if not precio_crudo:
        raise HTTPException(status_code=404, detail="No se pudo encontrar el precio con el selector provisto")

    # 2. Procesamiento y Limpieza del Dato (Nivel Senior)
    precio_actual = limpiar_precio(precio_crudo)
    fecha_actual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 3. Lógica de Base de Datos e Inteligencia de Alertas
    producto_existente = db["productos"].find_one({"url": request.url})

    if not producto_existente:
        # CAMINO A: El producto es nuevo, lo registramos en el catálogo maestro
        nuevo_producto = {
            "url": request.url,
            "selector": request.selector,
            "precio_actual_catalogo": precio_actual, # Guardado como float
            "ultima_actualizacion": fecha_actual
        }
        db["productos"].insert_one(nuevo_producto)
        
        return {
            "status": "success",
            "message": "Producto registrado por primera vez en el catálogo maestro.",
            "precio_detectado": precio_actual
        }
    
    else:
        # CAMINO B: El producto ya existe, evaluamos si hay un cambio de precio
        precio_anterior = producto_existente["precio_actual_catalogo"]
        
        # Guardamos el registro en la colección de históricos
        nuevo_historial = {
            "producto_id": producto_existente["_id"],
            "precio": precio_actual, # Guardado como float
            "fecha_captura": fecha_actual
        }
        db["historial_precios"].insert_one(nuevo_historial)

        # ¡AQUÍ ESTÁ LA MAGIA PARA TU PORTAFOLIO!
        # Evaluamos si el precio actual es estrictamente menor al que teníamos guardado
        if precio_actual < precio_anterior:
            # Simulamos el título del producto usando la URL o puedes extraerlo con un selector
            producto_titulo = request.url.split("/")[-2].replace("-", " ").title()
            
            # Disparamos la alerta automática
            enviar_alerta_discord(producto_titulo, precio_anterior, precio_actual, request.url)
            
            # Actualizamos el precio maestro en el catálogo para que sea el nuevo precio de referencia
            db["productos"].update_one(
                {"_id": producto_existente["_id"]},
                {"$set": {"precio_actual_catalogo": precio_actual, "ultima_actualizacion": fecha_actual}}
            )
            
            return {
                "status": "alerta",
                "message": "¡Oferta detectada! Alerta enviada al sistema de notificaciones.",
                "precio_anterior": precio_anterior,
                "precio_nuevo": precio_actual
            }

        # Si el precio es igual o subió, solo actualizamos la fecha de chequeo
        db["productos"].update_one(
            {"_id": producto_existente["_id"]},
            {"$set": {"ultima_actualizacion": fecha_actual}}
        )

        return {
            "status": "success",
            "message": "Historial actualizado. El precio no ha bajado.",
            "precio_actual": precio_actual
        }


@app.get("/price-history")
def get_price_history(url: str):
    producto = db["productos"].find_one({"url": url})
    
    if producto is None:
        return {"status": "error", "message": "No existe el producto en el catálogo"}
    
    # Buscamos en el historial usando el ObjectId puro de la relación
    historial_cursor = db["historial_precios"].find({"producto_id": producto["_id"]})
    
    lista_historial = []
    for registro in historial_cursor:
        lista_historial.append({
            "precio": registro["precio"],
            "fecha_capture": registro["fecha_captura"]
        })
        
    return {
        "status": 200,
        "producto": {
            "url": producto["url"],
            "selector": producto["selector"],
            "precio_actual_catalogo": producto["precio_actual_catalogo"]
        },
        "historial_de_precios": lista_historial
    }







 
