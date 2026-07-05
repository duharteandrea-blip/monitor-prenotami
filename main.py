import os
import urllib.request
import urllib.parse
import http.cookiejar
import smtplib
import ssl
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

# Todas las credenciales viven en variables de entorno de Railway.
# Nunca deben escribirse aqui en texto plano.
PRENOTAMI_EMAIL = os.environ.get("PRENOTAMI_EMAIL", "")
PRENOTAMI_PASSWORD = os.environ.get("PRENOTAMI_PASSWORD", "")
GMAIL_SENDER = os.environ.get("GMAIL_SENDER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
GMAIL_RECIPIENT = os.environ.get("GMAIL_RECIPIENT", GMAIL_SENDER)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
CHECK_INTERVAL_MIN = int(os.environ.get("CHECK_INTERVAL_MIN", "15"))

SERVICE_URL = "https://prenotami.esteri.it/Services"
LOGIN_URL = "https://prenotami.esteri.it/Home"
BOOK_URL = "https://prenotami.esteri.it/Services/Booking/1972"

BOOKED_PHRASES = [
    "all appointments for this service are currently booked",
    "sorry, all appointments",
    "fully booked",
    "no hay citas disponibles",
]

# Si alguna de estas seÃ±ales aparece, significa que NO estamos realmente
# logueados (nos devolviÃ³ la pantalla de login). En ese caso jamas debemos
# reportar "disponible" -- eso fue el bug que causo las falsas alarmas.
NOT_LOGGED_IN_MARKERS = [
    'name="password"',
    'id="password"',
    'accedi',
    'log in',
]


def log(msg):
    ts = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def send_ntfy(title, message):
    if not NTFY_TOPIC:
        return False
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("ascii", errors="ignore"),
                "Priority": "urgent",
                "Tags": "rotating_light",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=15)
        log("Notificacion push enviada (ntfy)")
        return True
    except Exception as e:
        log(f"Error al enviar push ntfy: {e}")
        return False


def send_email(subject, html_body):
    if not (GMAIL_SENDER and GMAIL_APP_PASSWORD and GMAIL_RECIPIENT):
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_SENDER
    msg["To"] = GMAIL_RECIPIENT
    msg.attach(MIMEText(html_body, "html"))

    # Intento 1: puerto 465 (SSL directo)
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=20) as server:
            server.login(GMAIL_SENDER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_SENDER, GMAIL_RECIPIENT, msg.as_string())
        log("Email enviado (puerto 465)")
        return True
    except Exception as e1:
        log(f"Fallo email puerto 465: {e1}")

    # Intento 2: puerto 587 (STARTTLS) -- algunos hosts en la nube bloquean 465
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as server:
            server.starttls(context=context)
            server.login(GMAIL_SENDER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_SENDER, GMAIL_RECIPIENT, msg.as_string())
        log("Email enviado (puerto 587)")
        return True
    except Exception as e2:
        log(f"Fallo email puerto 587: {e2}")
        return False


def send_alert(subject, body_text):
    html = f"""
    <html><body style="font-family:Arial,sans-serif;padding:20px;background:#f9f9f9">
    <div style="background:white;padding:30px;border-radius:10px;max-width:500px;margin:auto">
    <h2 style="color:#006400">Embajada Italia Lima</h2>
    <p style="font-size:16px;color:#333">{body_text}</p>
    <a href="{SERVICE_URL}"
    style="background:#0057A8;color:white;padding:14px 28px;text-decoration:none;
    border-radius:8px;font-size:16px;display:inline-block;margin-top:15px;font-weight:bold">
    RESERVAR AHORA
    </a>
    <p style="color:#999;font-size:12px;margin-top:25px">
    Entra rapido, las citas se agotan en minutos.<br>
    Monitor automatico 24/7 - Prenotami - Embajada Italia Lima
    </p>
    </div>
    </body></html>
    """
    ntfy_ok = send_ntfy(subject, body_text)
    email_ok = send_email(subject, html)
    if not ntfy_ok and not email_ok:
        log("ALERTA: no se pudo enviar ni push ni email. Revisa la configuracion.")
    return ntfy_ok or email_ok


def check_availability():
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [
        ("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
        ("Accept-Language", "es-ES,es;q=0.9,en;q=0.8"),
    ]

    resp = opener.open(LOGIN_URL, timeout=30)
    html = resp.read().decode("utf-8", errors="ignore")

    token = ""
    for line in html.split("\n"):
        if "__RequestVerificationToken" in line and "value=" in line:
            start = line.find('value="') + 7
            end = line.find('"', start)
            token = line[start:end]
            break

    data = urllib.parse.urlencode({
        "Email": PRENOTAMI_EMAIL,
        "Password": PRENOTAMI_PASSWORD,
        "__RequestVerificationToken": token,
    }).encode("utf-8")

    req = urllib.request.Request(LOGIN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Referer", LOGIN_URL)
    opener.open(req, timeout=30)

    resp2 = opener.open(BOOK_URL, timeout=30)
    content = resp2.read().decode("utf-8", errors="ignore")
    content_lower = content.lower()

    # FAIL-SAFE: si no podemos confirmar que el login funciono, NUNCA
    # reportamos "disponible". Esto es lo que causaba las falsas alarmas.
    if any(marker in content_lower for marker in NOT_LOGGED_IN_MARKERS):
        raise RuntimeError(
            "No se pudo confirmar el login en Prenotami (parece pantalla de "
            "login) -- no se reporta disponibilidad para evitar falsa alarma"
        )

    if len(content) < 500:
        raise RuntimeError(
            f"Respuesta sospechosamente corta ({len(content)} caracteres) -- "
            "no se reporta disponibilidad"
        )

    for phrase in BOOKED_PHRASES:
        if phrase in content_lower:
            return False
    return True


def main():
    log("=" * 50)
    log("Monitor Prenotami 24/7 - Embajada Italia Lima")
    log("=" * 50)
    log(f"Revisando cada {CHECK_INTERVAL_MIN} minutos")
    log(f"Alertas push (ntfy): {'activado' if NTFY_TOPIC else 'NO configurado'}")
    log(f"Alertas email: {'activado' if GMAIL_SENDER and GMAIL_APP_PASSWORD else 'NO configurado'}")
    log("-" * 50)

    send_alert(
        "Monitor Prenotami activo 24/7",
        f"El monitor esta corriendo en la nube y revisara cada {CHECK_INTERVAL_MIN} "
        "minutos, incluso mientras duermes. Te avisare en cuanto haya citas para "
        "Ciudadania italiana jure sanguinis en la Embajada de Lima."
    )

    consecutive_errors = 0

    while True:
        try:
            available = check_availability()
            if available:
                log("*** CITAS DISPONIBLES! Enviando alerta urgente... ***")
                send_alert(
                    "URGENTE: CITA DISPONIBLE - Embajada Italia Lima",
                    "Se detecto disponibilidad para Ciudadania italiana jure "
                    "sanguinis. Entra AHORA antes de que se agoten:"
                )
                log("Esperando 5 minutos y vuelvo a revisar...")
                time.sleep(300)
            else:
                log(f"Sin disponibilidad -- proxima revision en {CHECK_INTERVAL_MIN} min")
                consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            log(f"Error ({consecutive_errors}): {e}")
            if consecutive_errors >= 10:
                log("Muchos errores. Reintentando en 30 min...")
                consecutive_errors = 0
                time.sleep(1800)

        time.sleep(CHECK_INTERVAL_MIN * 60)


if __name__ == "__main__":
    main()
