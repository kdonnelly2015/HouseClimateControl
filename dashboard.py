import tkinter as tk
import paho.mqtt.client as mqtt
import math
import requests
import datetime
import csv
import os

# --- Telegram Configuration ---
TELEGRAM_TOKEN = "8828747525:AAEEEWWp9DOxTGJ8WL0s3wLrDwCYTZtMZtI"
CHAT_IDS = ["8789981851", "8248273321"]

# --- State Tracking (Prevents Spam Messaging) ---
window_advice = None  # Can be "Open", "Close_Heat", "Close_AC", or "Close"
last_19c_warning_date = None  # Tracks the date of the last 19°C warning to ensure it fires once a day
is_too_hot = False  # Tracks if the day got too hot, waiting to cool down

# --- Logging Configuration ---
LOG_FILE = "sensor_data_log.csv"

# Global dictionary to hold our data cache
data_cache = {
    "indoor_temp": None,
    "indoor_humi": None,
    "outdoor_temp": None,
    "outdoor_humi": None
}

# --- Seasonal Definition ---
COLD_SEASON_MONTHS = [1, 2, 3, 4, 10, 11, 12]
WARM_SEASON_MONTHS = [5, 6, 7, 8, 9]


# --- Telegram Helper ---
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    for chat_id in CHAT_IDS:
        payload = {"chat_id": chat_id, "text": message}
        try:
            requests.post(url, json=payload, timeout=5)
        except Exception as e:
            print(f"Failed to send Telegram message to {chat_id}: {e}")


# --- Dew Point Calculation Function ---
def calculate_dew_point(temp, humi):
    if temp is None or humi is None:
        return None
    try:
        T = float(temp)
        RH = float(humi)
        a = 17.625
        b = 243.04
        alpha = ((a * T) / (b + T)) + math.log(RH / 100.0)
        return round((b * alpha) / (a - alpha), 1)
    except:
        return None


# --- Main Logic Evaluator ---
def evaluate_smart_rules():
    global window_advice, last_19c_warning_date, is_too_hot

    in_temp_raw = data_cache["indoor_temp"]
    in_humi_raw = data_cache["indoor_humi"]
    out_temp_raw = data_cache["outdoor_temp"]
    out_humi_raw = data_cache["outdoor_humi"]

    # Calculate Dew Points
    in_dew = calculate_dew_point(in_temp_raw, in_humi_raw)
    out_dew = calculate_dew_point(out_temp_raw, out_humi_raw)

    # Convert payloads to floats safely
    try:
        t_in = float(in_temp_raw) if in_temp_raw is not None else None
        h_in = float(in_humi_raw) if in_humi_raw is not None else None
        t_out = float(out_temp_raw) if out_temp_raw is not None else None
        h_out = float(out_humi_raw) if out_humi_raw is not None else None
    except (ValueError, TypeError):
        t_in, h_in, t_out, h_out = None, None, None, None

    # 1. Rule: Open/Close Windows based on Season and Comfort Targets
    if all(v is not None for v in [t_in, h_in, t_out, h_out, in_dew, out_dew]):

        new_advice_state = ""
        telegram_msg = ""
        ui_text = ""
        ui_fg = ""
        ui_bg = ""

        current_month = datetime.date.today().month

        # ==========================================
        # COLD SEASON LOGIC (Goal: Keep the house warm)
        # ==========================================
        if current_month in COLD_SEASON_MONTHS:
            if t_in < 18.0:
                # Too cold inside
                if t_out > t_in:
                    new_advice_state = "Open"
                    telegram_msg = "🍃 Free heat! It is warmer outside. Open windows, turn heating off."
                    ui_text = "🍃 WINDOWS OPEN\n(Free Heat Outside)"
                    ui_fg = "#4ade80"
                    ui_bg = "#142d14"
                else:
                    new_advice_state = "Close_Heat"
                    telegram_msg = "🔥 It is cold! Close windows, keep heat trapped (or turn heater on)."
                    ui_text = "🔥 CLOSE WINDOWS\n(Keep Warmth Trapped)"
                    ui_fg = "#ff6b6b"
                    ui_bg = "#2d1414"
            elif t_in > 22.0:
                # Oddly hot inside during winter (cooking, fireplace, etc.)
                if t_out < t_in:
                    new_advice_state = "Open"
                    telegram_msg = "🍃 Cooling alert! It's unusually warm inside. Open windows to vent."
                    ui_text = "🍃 WINDOWS OPEN\n(Vent Excess Heat)"
                    ui_fg = "#4ade80"
                    ui_bg = "#142d14"
                else:
                    new_advice_state = "Close"
                    telegram_msg = "⚠️ Unusually hot outside too. Close windows."
                    ui_text = "⚠️ KEEP WINDOWS CLOSED"
                    ui_fg = "#ffa44a"
                    ui_bg = "#2d1414"
            else:
                # Optimal indoor temps (18-22C)
                if 18.0 <= t_out <= 22.0 and h_out <= 60.0:
                    new_advice_state = "Open"
                    telegram_msg = "🍃 Pleasant outside! Open windows for fresh air without losing heat."
                    ui_text = "🍃 WINDOWS OPEN\n(Nice Weather)"
                    ui_fg = "#4ade80"
                    ui_bg = "#142d14"
                else:
                    new_advice_state = "Close"
                    telegram_msg = "⚠️ Close windows to maintain our perfect indoor temperature."
                    ui_text = "⚠️ KEEP WINDOWS CLOSED\n(Protect Indoor Temp)"
                    ui_fg = "#ffa44a"
                    ui_bg = "#2d1414"

        # ==========================================
        # WARM SEASON LOGIC (Goal: Keep the house cool)
        # ==========================================
        elif current_month in WARM_SEASON_MONTHS:
            if t_in > 22.0 or h_in > 60.0:
                # Too warm or too muggy inside
                if t_out < t_in and out_dew < in_dew:
                    new_advice_state = "Open"
                    telegram_msg = "🍃 Free cooling! Outside is cooler and drier. Open windows, AC off."
                    ui_text = "🍃 WINDOWS OPEN\n(Cool & Dry Outside)"
                    ui_fg = "#4ade80"
                    ui_bg = "#142d14"
                else:
                    new_advice_state = "Close_AC"
                    telegram_msg = "❄️ Muggy/Hot alert! Close windows, turn AC on to reach target temp."
                    ui_text = "❄️ CLOSE WINDOWS\n(Block Heat/Humidity)"
                    ui_fg = "#74b9ff"
                    ui_bg = "#14222d"
            elif t_in < 18.0:
                # Unusually cold inside during summer
                if t_out > t_in:
                    new_advice_state = "Open"
                    telegram_msg = "🍃 A bit chilly inside! Open windows to let the summer warmth in."
                    ui_text = "🍃 WINDOWS OPEN\n(Let Warmth In)"
                    ui_fg = "#4ade80"
                    ui_bg = "#142d14"
                else:
                    new_advice_state = "Close"
                    telegram_msg = "⚠️ Cold outside too. Close windows."
                    ui_text = "⚠️ KEEP WINDOWS CLOSED"
                    ui_fg = "#ffa44a"
                    ui_bg = "#2d1414"
            else:
                # Optimal indoor temps (18-22C)
                if t_out > 22.0 or out_dew > 14.0 or h_out > 60.0:
                    new_advice_state = "Close"
                    telegram_msg = "⚠️ Close windows! It's getting hot/muggy outside. Trap our cool air inside."
                    ui_text = "⚠️ KEEP WINDOWS CLOSED\n(Protect Indoor Temp)"
                    ui_fg = "#ffa44a"
                    ui_bg = "#2d1414"
                else:
                    new_advice_state = "Open"
                    telegram_msg = "🍃 Pleasant outside! Open windows for fresh summer air."
                    ui_text = "🍃 WINDOWS OPEN\n(Nice Weather)"
                    ui_fg = "#4ade80"
                    ui_bg = "#142d14"

        # Apply State Changes (Notify Telegram only on change)
        if window_advice != new_advice_state:
            window_advice = new_advice_state
            send_telegram(telegram_msg)

        # Update Tkinter UI Elements
        lbl_advice.config(text=ui_text, fg=ui_fg, bg=ui_bg)
        frame_advice.config(bg=ui_bg)

    # 2. Rule: Dog Walking Alerts
    if t_out is not None:
        today = datetime.date.today()

        if 19.0 <= t_out < 22.0:
            if last_19c_warning_date != today:
                last_19c_warning_date = today
                send_telegram(
                    f"☀️ Warming up! It's currently {t_out}°C outside. Walk Kizzy soon before it reaches 22°C.")

        if t_out >= 22.5:
            is_too_hot = True

        elif t_out <= 22.0 and is_too_hot:
            is_too_hot = False
            send_telegram(f"🐕 Safe to walk Kizzy! The temperature has cooled down to {t_out}°C.")


# --- MQTT Callback ---
def on_message(client, userdata, message):
    payload = message.payload.decode("utf-8")
    topic = message.topic

    if topic == "home/indoor/temperature":
        data_cache["indoor_temp"] = payload
    elif topic == "home/indoor/humidity":
        data_cache["indoor_humi"] = payload
    elif topic == "home/outdoor/temperature":
        data_cache["outdoor_temp"] = payload
    elif topic == "home/outdoor/humidity":
        data_cache["outdoor_humi"] = payload

    evaluate_smart_rules()

    in_dew = calculate_dew_point(data_cache["indoor_temp"], data_cache["indoor_humi"])
    out_dew = calculate_dew_point(data_cache["outdoor_temp"], data_cache["outdoor_humi"])

    lbl_in_temp.config(text=f"{data_cache['indoor_temp'] or '--.-'} °C")
    lbl_in_humi.config(text=f"Humidity: {data_cache['indoor_humi'] or '--'}%")
    lbl_in_dew.config(text=f"Dew Point: {in_dew if in_dew is not None else '--.-'} °C")

    lbl_out_temp.config(text=f"{data_cache['outdoor_temp'] or '--.-'} °C")
    lbl_out_humi.config(text=f"Humidity: {data_cache['outdoor_humi'] or '--'}%")
    lbl_out_dew.config(text=f"Dew Point: {out_dew if out_dew is not None else '--.-'} °C")


# --- Data Logging Functions ---
def init_log_file():
    """Creates the CSV file with headers if it doesn't exist."""
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, mode='w', newline='') as file:
            writer = csv.writer(file)
            writer.writerow(["Timestamp", "Indoor_Temp", "Indoor_Hum", "Outdoor_Temp", "Outdoor_Hum"])


def log_sensor_data():
    """Logs the current cache to the CSV file and schedules the next run."""
    now = datetime.datetime.now()
    timestamp = now.strftime("%Y-%m-%d %H:%M:00")

    with open(LOG_FILE, mode='a', newline='') as file:
        writer = csv.writer(file)
        writer.writerow([
            timestamp,
            data_cache["indoor_temp"],
            data_cache["indoor_humi"],
            data_cache["outdoor_temp"],
            data_cache["outdoor_humi"]
        ])

    # Schedule the next reading
    schedule_next_log()


def schedule_next_log():
    """Calculates milliseconds until the next 00 or 30 minute mark."""
    now = datetime.datetime.now()

    if now.minute < 30:
        target = now.replace(minute=30, second=0, microsecond=0)
    else:
        target = (now + datetime.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)

    delay_ms = int((target - now).total_seconds() * 1000)
    root.after(delay_ms, log_sensor_data)


# --- UI Setup ---
root = tk.Tk()
root.title("Smart Home Hub")
root.configure(bg="#121212")
root.attributes('-fullscreen', True)

frame_indoor = tk.Frame(root, bg="#1a1a1a", bd=2, relief="groove")
frame_indoor.place(relx=0.04, rely=0.04, relwidth=0.44, relheight=0.7)

tk.Label(frame_indoor, text="INDOOR", font=("Helvetica", 12, "bold"), fg="#3498db", bg="#1a1a1a").pack(pady=4)
lbl_in_temp = tk.Label(frame_indoor, text="--.- °C", font=("Helvetica", 20, "bold"), fg="white", bg="#1a1a1a")
lbl_in_temp.pack(pady=4)
lbl_in_humi = tk.Label(frame_indoor, text="Humidity: --%", font=("Helvetica", 10), fg="#aaaaaa", bg="#1a1a1a")
lbl_in_humi.pack()
lbl_in_dew = tk.Label(frame_indoor, text="Dew Point: --.- °C", font=("Helvetica", 10, "italic"), fg="#85c1e9",
                      bg="#1a1a1a")
lbl_in_dew.pack(pady=4)

frame_outdoor = tk.Frame(root, bg="#1a1a1a", bd=2, relief="groove")
frame_outdoor.place(relx=0.52, rely=0.04, relwidth=0.44, relheight=0.7)

tk.Label(frame_outdoor, text="OUTDOOR", font=("Helvetica", 12, "bold"), fg="#2ecc71", bg="#1a1a1a").pack(pady=4)
lbl_out_temp = tk.Label(frame_outdoor, text="--.- °C", font=("Helvetica", 20, "bold"), fg="white", bg="#1a1a1a")
lbl_out_temp.pack(pady=4)
lbl_out_humi = tk.Label(frame_outdoor, text="Humidity: --%", font=("Helvetica", 10), fg="#aaaaaa", bg="#1a1a1a")
lbl_out_humi.pack()
lbl_out_dew = tk.Label(frame_outdoor, text="Dew Point: --.- °C", font=("Helvetica", 10, "italic"), fg="#a3e4d7",
                       bg="#1a1a1a")
lbl_out_dew.pack(pady=4)

frame_advice = tk.Frame(root, bg="#1f1f1f", bd=2, relief="groove")
frame_advice.place(relx=0.04, rely=0.78, relwidth=0.92, relheight=0.18)

lbl_advice = tk.Label(frame_advice, text="Awaiting sensor readings...", font=("Helvetica", 11, "bold"), fg="#888888",
                      bg="#1f1f1f")
lbl_advice.pack(expand=True, fill="both")

root.bind("<Escape>", lambda e: root.destroy())

# --- Initialization & Background Setup ---
init_log_file()
schedule_next_log()

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_message = on_message
client.connect("localhost", 1883)
client.subscribe("home/+/temperature")
client.subscribe("home/+/humidity")
client.loop_start()

root.mainloop()