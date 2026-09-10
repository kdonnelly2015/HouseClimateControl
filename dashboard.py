import tkinter as tk
import paho.mqtt.client as mqtt
import math
import requests
import datetime
import csv
import os
import threading

from flask import Flask, jsonify, request, Response

# --- Telegram Configuration ---
# NOTE: this token was shared in plain text - regenerate it via BotFather and
# ideally load it from an environment variable instead of hardcoding it.
TELEGRAM_TOKEN = "8828747525:AAEEEWWp9DOxTGJ8WL0s3wLrDwCYTZtMZtI"
CHAT_IDS = ["8789981851", "8248273321"]

# --- Web Dashboard Configuration ---
# Serves a read-mostly copy of this UI to any browser on the LAN.
# Visit http://<pi-ip>:WEB_PORT from any phone/laptop on the same network.
WEB_HOST = "0.0.0.0"
WEB_PORT = 8080

# --- State Tracking (Prevents Spam Messaging) ---
window_advice = None
is_too_hot = False

# Dog Walking & Forecast States
last_19c_warning_date = None
last_forecast_date = None
forecasted_max_temp = None
cool_day_notified = False

# Rain States (Weather/BME280 sensor)
rain_alert_active = False   # true while a pressure-drop rain warning is "live"
currently_raining = False   # true while Open-Meteo reports active precipitation
pressure_history = []       # list of (datetime, pressure_hpa) for trend detection

# --- Confirmation / Debounce Configuration ---
# Nothing fires on a single reading. A condition has to still be true when it is
# re-evaluated at least CONFIRMATION_SECONDS later, using a fresh sensor reading.
CONFIRMATION_SECONDS = 5 * 60

# key -> {"value": <candidate state>, "since": datetime, "readings": <snapshot>}
pending_states = {}

# The last advice we actually committed to, so the screen keeps showing it while
# a change is still being confirmed.
committed_ui = None

# Plain-data mirror of whatever is currently on screen in the advice panel.
# Kept separate from the Tkinter widget so the web dashboard (running in a
# different thread) never has to touch Tkinter objects directly.
latest_advice_display = {"text": "Awaiting confirmed readings...", "fg": "#888888", "bg": "#1f1f1f"}

# Timestamp of the last MQTT message we processed, shown on the web dashboard
# so remote viewers can tell if data has gone stale.
last_update_time = None

# --- Hourly Sensor Report Configuration ---
HOURLY_REPORT_START_HOUR = 8    # first report of the day (08:00)
HOURLY_REPORT_END_HOUR = 21     # last report of the day (21:00 = 9pm)
hourly_reports_enabled = False  # controlled by the toggle switch in the header

# --- Logging Configuration ---
LOG_FILE = "sensor_data_log.csv"
# NOTE: if sensor_data_log.csv already exists from before the Weather sensor was
# added, its header row won't include the new Weather columns (the file is only
# created fresh if it's missing). Rename/delete the old file once if you want a
# clean header, or just accept blank Weather columns for old rows.

# Global dictionary to hold our data cache
data_cache = {
    "indoor_temp": None,
    "indoor_humi": None,
    "outdoor_temp": None,
    "outdoor_humi": None,
    "weather_temp": None,
    "weather_humi": None,
    "weather_pres": None,
}

# --- Seasonal Definition ---
COLD_SEASON_MONTHS = [1, 2, 3, 4, 10, 11, 12]
WARM_SEASON_MONTHS = [5, 6, 7, 8, 9]

# --- Rain Prediction Configuration ---
# Classic ship's-barometer rule of thumb: a fall of ~3+ hPa over 3 hours signals
# rain is likely within the next few hours; 6+ hPa signals a stronger system
# (storm-grade) is approaching. The BME280 can only measure pressure, not actual
# precipitation, so this is an *inference*, not a direct rain reading.
RAIN_TREND_WINDOW_HOURS = 3
RAIN_FALL_THRESHOLD_HPA = -3.0
STORM_FALL_THRESHOLD_HPA = -6.0
RAIN_TREND_RESET_HPA = -1.5  # trend must recover above this before we'll alert again


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


# --- Weather API Helper ---
def get_daily_max_temp():
    """Fetches today's forecasted maximum temperature for Southampton."""
    url = "https://api.open-meteo.com/v1/forecast?latitude=50.9039&longitude=-1.4043&daily=temperature_2m_max&timezone=Europe%2FLondon&forecast_days=1"
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        max_temp = data['daily']['temperature_2m_max'][0]
        return float(max_temp)
    except Exception as e:
        print(f"Failed to fetch weather forecast: {e}")
        return None


def get_current_precipitation():
    """Fallback rain check: the BME280 can't sense rain itself, so this asks
    Open-Meteo for its current (radar/model-based) precipitation estimate for
    Southampton. Returns mm in the last hour, or None if the request failed."""
    url = "https://api.open-meteo.com/v1/forecast?latitude=50.9039&longitude=-1.4043&current=precipitation&timezone=Europe%2FLondon"
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        return float(data['current']['precipitation'])
    except Exception as e:
        print(f"Failed to fetch current precipitation: {e}")
        return None


# --- Pressure Trend Helper (Weather/BME280 sensor) ---
def record_pressure_reading(pressure_hpa):
    """Appends a timestamped pressure reading and prunes anything older than we need."""
    global pressure_history
    now = datetime.datetime.now()
    pressure_history.append((now, pressure_hpa))
    cutoff = now - datetime.timedelta(hours=RAIN_TREND_WINDOW_HOURS, minutes=30)
    pressure_history = [(t, p) for (t, p) in pressure_history if t >= cutoff]


def get_pressure_trend_hpa():
    """Change in pressure (hPa) from the oldest reading within the trend window
    to the most recent one. Negative = falling. Returns None if we don't have
    enough history yet (e.g. just after startup)."""
    if len(pressure_history) < 2:
        return None

    now = datetime.datetime.now()
    window_start = now - datetime.timedelta(hours=RAIN_TREND_WINDOW_HOURS)
    in_window = [(t, p) for (t, p) in pressure_history if t >= window_start]

    if len(in_window) < 2:
        return None

    oldest_p = in_window[0][1]
    newest_p = pressure_history[-1][1]
    return round(newest_p - oldest_p, 1)


# --- Confirmation Helpers ---
def snapshot_readings():
    """Freezes the current cache so we can compare against it 5 minutes later."""
    return {
        "at": datetime.datetime.now(),
        "indoor_temp": data_cache["indoor_temp"],
        "indoor_humi": data_cache["indoor_humi"],
        "outdoor_temp": data_cache["outdoor_temp"],
        "outdoor_humi": data_cache["outdoor_humi"],
    }


def format_snapshot(snap):
    def fmt(value):
        return value if value is not None else "--"

    return (f"{snap['at'].strftime('%H:%M')}  "
            f"in {fmt(snap['indoor_temp'])}°C/{fmt(snap['indoor_humi'])}%  "
            f"out {fmt(snap['outdoor_temp'])}°C/{fmt(snap['outdoor_humi'])}%")


def confirmation_footer(first_reading):
    """Shows both readings that agreed, so the alert is auditable."""
    return ("\n\n[Confirmed over 5 min:\n"
            f"  first  -> {format_snapshot(first_reading)}\n"
            f"  latest -> {format_snapshot(snapshot_readings())}]")


def confirmed_reading(key, value):
    """Hold a condition for CONFIRMATION_SECONDS before allowing it to act.

    The first time `value` is seen for `key`, the current readings are stashed
    and nothing happens. Once the same `value` is still being produced by a
    later reading at least CONFIRMATION_SECONDS after that first one, the stashed
    snapshot is returned (truthy) and the caller may act. Any other value seen in
    between resets the timer, so a one-off spike never gets through.

    Returns the first reading's snapshot on confirmation, otherwise None.
    """
    now = datetime.datetime.now()
    pending = pending_states.get(key)

    if pending is None or pending["value"] != value:
        pending_states[key] = {
            "value": value,
            "since": now,
            "readings": snapshot_readings(),
        }
        return None

    if (now - pending["since"]).total_seconds() >= CONFIRMATION_SECONDS:
        del pending_states[key]
        return pending["readings"]

    return None


def clear_pending(key):
    pending_states.pop(key, None)


def pending_minutes_left(key):
    """Whole minutes (rounded up) until the pending change for `key` confirms."""
    pending = pending_states.get(key)
    if pending is None:
        return None

    elapsed = (datetime.datetime.now() - pending["since"]).total_seconds()
    remaining = max(CONFIRMATION_SECONDS - elapsed, 0)
    return max(int(remaining // 60) + (1 if remaining % 60 else 0), 1)


def render_advice(ui, note=None):
    """Paints the advice panel. `ui` is the last confirmed advice (or None).

    Also mirrors the result into `latest_advice_display`, a plain dict that the
    web dashboard reads from instead of touching Tkinter widgets directly.
    """
    global latest_advice_display

    if ui is None:
        text, fg, bg = "Awaiting confirmed readings...", "#888888", "#1f1f1f"
    else:
        text, fg, bg = ui["text"], ui["fg"], ui["bg"]

    if note:
        text = f"{text}\n{note}"

    lbl_advice.config(text=text, fg=fg, bg=bg)
    frame_advice.config(bg=bg)

    latest_advice_display = {"text": text, "fg": fg, "bg": bg}


# --- Custom Toggle Switch Widget ---
class ToggleSwitch(tk.Canvas):
    """A finger-friendly sliding on/off switch drawn on a Canvas."""

    def __init__(self, parent, width=76, height=36, on_color="#4ade80",
                 off_color="#4a4a4a", knob_color="#ffffff", bg="#121212",
                 command=None, initial=False):
        super().__init__(parent, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0)
        self.sw_width = width
        self.sw_height = height
        self.on_color = on_color
        self.off_color = off_color
        self.knob_color = knob_color
        self.command = command
        self.state_on = bool(initial)

        self.bind("<Button-1>", self.toggle)
        self._draw()

    def _draw_track(self, color):
        r = self.sw_height / 2
        self.create_oval(0, 0, self.sw_height, self.sw_height,
                         fill=color, outline=color)
        self.create_oval(self.sw_width - self.sw_height, 0,
                         self.sw_width, self.sw_height,
                         fill=color, outline=color)
        self.create_rectangle(r, 0, self.sw_width - r, self.sw_height,
                              fill=color, outline=color)

    def _draw(self):
        self.delete("all")
        self._draw_track(self.on_color if self.state_on else self.off_color)

        pad = 4
        d = self.sw_height - (pad * 2)
        x0 = (self.sw_width - pad - d) if self.state_on else pad
        self.create_oval(x0, pad, x0 + d, pad + d,
                         fill=self.knob_color, outline=self.knob_color)

    def toggle(self, event=None):
        self.state_on = not self.state_on
        self._draw()
        if self.command:
            self.command(self.state_on)

    def set(self, value):
        self.state_on = bool(value)
        self._draw()
        if self.command:
            self.command(self.state_on)

    def get(self):
        return self.state_on


# --- Main Logic Evaluator ---
def evaluate_smart_rules():
    global window_advice, is_too_hot, committed_ui
    global last_19c_warning_date, last_forecast_date, forecasted_max_temp, cool_day_notified
    global rain_alert_active

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

    today = datetime.date.today()
    now = datetime.datetime.now()

    # ==========================================
    # 1. Fetch Daily Forecast (Once a day)
    # ==========================================
    if last_forecast_date != today:
        max_temp = get_daily_max_temp()
        if max_temp is not None:
            forecasted_max_temp = max_temp
            last_forecast_date = today
            cool_day_notified = False
            last_19c_warning_date = None
            is_too_hot = False
            clear_pending("walk_soon")
            clear_pending("outdoor_hot")

    # ==========================================
    # 2. Window Logic (Seasonal)
    # ==========================================
    if all(v is not None for v in [t_in, h_in, t_out, h_out, in_dew, out_dew]):
        new_advice_state = ""
        telegram_msg = ""
        ui_text = ""
        ui_fg = ""
        ui_bg = ""

        current_month = now.month

        # COLD SEASON LOGIC
        if current_month in COLD_SEASON_MONTHS:
            if t_in < 18.0:
                if t_out > t_in:
                    new_advice_state = "Open"
                    telegram_msg = "🍃 Free heat! It is warmer outside. Open windows, turn heating off.\n\n[Condition: COLD_SEASON | t_in < 18.0 | t_out > t_in]"
                    ui_text = "🍃 WINDOWS OPEN\n(Free Heat Outside)"
                    ui_fg = "#4ade80"
                    ui_bg = "#142d14"
                else:
                    new_advice_state = "Close_Heat"
                    telegram_msg = "🔥 It is cold! Close windows, keep heat trapped (or turn heater on).\n\n[Condition: COLD_SEASON | t_in < 18.0 | t_out <= t_in]"
                    ui_text = "🔥 CLOSE WINDOWS\n(Keep Warmth Trapped)"
                    ui_fg = "#ff6b6b"
                    ui_bg = "#2d1414"
            elif t_in > 22.0:
                if t_out < t_in:
                    new_advice_state = "Open"
                    telegram_msg = "🍃 Cooling alert! It's unusually warm inside. Open windows to vent.\n\n[Condition: COLD_SEASON | t_in > 22.0 | t_out < t_in]"
                    ui_text = "🍃 WINDOWS OPEN\n(Vent Excess Heat)"
                    ui_fg = "#4ade80"
                    ui_bg = "#142d14"
                else:
                    new_advice_state = "Close"
                    telegram_msg = "⚠️ Unusually hot outside too. Close windows.\n\n[Condition: COLD_SEASON | t_in > 22.0 | t_out >= t_in]"
                    ui_text = "⚠️ KEEP WINDOWS CLOSED"
                    ui_fg = "#ffa44a"
                    ui_bg = "#2d1414"
            else:
                if 18.0 <= t_out <= 22.0 and h_out <= 60.0:
                    new_advice_state = "Open"
                    telegram_msg = "🍃 Pleasant outside! Open windows for fresh air without losing heat.\n\n[Condition: COLD_SEASON | 18.0 <= t_in <= 22.0 | 18.0 <= t_out <= 22.0 & h_out <= 60.0]"
                    ui_text = "🍃 WINDOWS OPEN\n(Nice Weather)"
                    ui_fg = "#4ade80"
                    ui_bg = "#142d14"
                else:
                    new_advice_state = "Close"
                    telegram_msg = "⚠️ Close windows to maintain our perfect indoor temperature.\n\n[Condition: COLD_SEASON | 18.0 <= t_in <= 22.0 | Outdoor temp/humidity not optimal]"
                    ui_text = "⚠️ KEEP WINDOWS CLOSED\n(Protect Indoor Temp)"
                    ui_fg = "#ffa44a"
                    ui_bg = "#2d1414"

        # WARM SEASON LOGIC
        elif current_month in WARM_SEASON_MONTHS:
            if t_in > 22.0 or h_in > 60.0:
                if t_out < t_in and out_dew < in_dew:
                    new_advice_state = "Open"
                    telegram_msg = "🍃 Free cooling! Outside is cooler and drier. Open windows, AC off.\n\n[Condition: WARM_SEASON | t_in > 22.0 or h_in > 60.0 | t_out < t_in & out_dew < in_dew]"
                    ui_text = "🍃 WINDOWS OPEN\n(Cool & Dry Outside)"
                    ui_fg = "#4ade80"
                    ui_bg = "#142d14"
                else:
                    new_advice_state = "Close_AC"
                    telegram_msg = "❄️ Muggy/Hot alert! Close windows, turn AC on to reach target temp.\n\n[Condition: WARM_SEASON | t_in > 22.0 or h_in > 60.0 | t_out >= t_in or out_dew >= in_dew]"
                    ui_text = "❄️ CLOSE WINDOWS\n(Block Heat/Humidity)"
                    ui_fg = "#74b9ff"
                    ui_bg = "#14222d"
            elif t_in < 18.0:
                if t_out > t_in:
                    new_advice_state = "Open"
                    telegram_msg = "🍃 A bit chilly inside! Open windows to let the summer warmth in.\n\n[Condition: WARM_SEASON | t_in < 18.0 | t_out > t_in]"
                    ui_text = "🍃 WINDOWS OPEN\n(Let Warmth In)"
                    ui_fg = "#4ade80"
                    ui_bg = "#142d14"
                else:
                    new_advice_state = "Close"
                    telegram_msg = "⚠️ Cold outside too. Close windows.\n\n[Condition: WARM_SEASON | t_in < 18.0 | t_out <= t_in]"
                    ui_text = "⚠️ KEEP WINDOWS CLOSED"
                    ui_fg = "#ffa44a"
                    ui_bg = "#2d1414"
            else:
                if t_out > 22.0 or out_dew > 14.0 or h_out > 60.0:
                    new_advice_state = "Close"
                    telegram_msg = "⚠️ Close windows! It's getting hot/muggy outside. Trap our cool air inside.\n\n[Condition: WARM_SEASON | if t_out > 22.0 or out_dew > 14.0 or h_out > 60.0]"
                    ui_text = "⚠️ KEEP WINDOWS CLOSED\n(Protect Indoor Temp)"
                    ui_fg = "#ffa44a"
                    ui_bg = "#2d1414"
                else:
                    new_advice_state = "Open"
                    telegram_msg = "🍃 Pleasant outside! Open windows for fresh summer air.\n\n[Condition: WARM_SEASON | 18.0 <= t_in <= 22.0 & h_in <= 60.0 | Outdoor conditions optimal]"
                    ui_text = "🍃 WINDOWS OPEN\n(Nice Weather)"
                    ui_fg = "#4ade80"
                    ui_bg = "#142d14"

        fresh_ui = {"text": ui_text, "fg": ui_fg, "bg": ui_bg}

        # Apply State Changes (only once the same advice has held for 5 minutes)
        if new_advice_state == window_advice:
            # Nothing is changing - drop any half-finished countdown.
            clear_pending("window")
            committed_ui = fresh_ui
            render_advice(committed_ui)
        else:
            first_reading = confirmed_reading("window", new_advice_state)
            if first_reading:
                window_advice = new_advice_state
                send_telegram(telegram_msg + confirmation_footer(first_reading))
                committed_ui = fresh_ui
                render_advice(committed_ui)
            else:
                minutes = pending_minutes_left("window")
                render_advice(committed_ui, f"⏳ Confirming change… ({minutes} min)")

    # ==========================================
    # 3. Dog Walking Alerts (Forecast & Real-time)
    # ==========================================
    if forecasted_max_temp is not None:

        # Scenario A: The day is staying cool
        # (Forecast-driven, not a sensor threshold, so no confirmation needed.)
        if forecasted_max_temp <= 23.0 and now.hour >= 8 and not cool_day_notified:
            send_telegram(
                f"☁️ It's staying cool today (Forecast max: {forecasted_max_temp}°C). Walk Kizzy whenever she demands it!\n\n[Condition: forecasted_max_temp <= 23.0 & time >= 08:00]")
            cool_day_notified = True

        # Scenario B: The day is going to get hot
        if forecasted_max_temp > 23.0 and t_out is not None:
            if 19.0 <= t_out < 22.0 and last_19c_warning_date != today:
                first_reading = confirmed_reading("walk_soon", today.isoformat())
                if first_reading:
                    last_19c_warning_date = today
                    send_telegram(
                        f"☀️ Warming up! It's currently {t_out}°C outside (Forecast max: {forecasted_max_temp}°C). Walk Kizzy soon before it reaches 22°C.\n\n[Condition: forecasted_max_temp > 23.0 & 19.0 <= t_out < 22.0 & not_alerted_today]"
                        + confirmation_footer(first_reading))
            else:
                clear_pending("walk_soon")
        else:
            clear_pending("walk_soon")

    # Absolute failsafe (because UK weather forecasts are sometimes wrong)
    # Both the "gone hot" and "cooled down" transitions have to survive 5 minutes,
    # so a single spiky reading can't flip the flag either way.
    if t_out is not None:
        if t_out >= 22.5 and not is_too_hot:
            if confirmed_reading("outdoor_hot", "hot"):
                is_too_hot = True
        elif t_out <= 22.0 and is_too_hot:
            first_reading = confirmed_reading("outdoor_hot", "cool")
            if first_reading:
                is_too_hot = False
                send_telegram(
                    f"🐕 Safe to walk Kizzy again! The temperature has cooled down to {t_out}°C.\n\n[Condition: t_out <= 22.0 & is_too_hot_flag_was_true]"
                    + confirmation_footer(first_reading))
        else:
            clear_pending("outdoor_hot")

    # ==========================================
    # 4. Rain Prediction (Weather/BME280 pressure trend)
    # ==========================================
    # The BME280 can only measure pressure - it has no way to sense rain
    # directly. A sustained fall over a few hours is a reasonable leading
    # indicator, so we treat it the same way as the other confirmed alerts:
    # the trend has to hold for CONFIRMATION_SECONDS before we act, and once
    # we've alerted we stay quiet until pressure recovers (hysteresis), so we
    # don't send a new warning every few minutes while it's still falling.
    trend = get_pressure_trend_hpa()
    if trend is not None:
        if trend <= RAIN_FALL_THRESHOLD_HPA and not rain_alert_active:
            first_reading = confirmed_reading("rain_pressure_drop", "falling")
            if first_reading:
                rain_alert_active = True
                if trend <= STORM_FALL_THRESHOLD_HPA:
                    icon, label = "⛈️", "Storm possible"
                else:
                    icon, label = "🌧️", "Rain likely"
                send_telegram(
                    f"{icon} {label} - barometric pressure has fallen {abs(trend)} hPa "
                    f"in the last {RAIN_TREND_WINDOW_HOURS} hours (Weather sensor).\n\n"
                    f"[Condition: BME280 pressure trend <= {RAIN_FALL_THRESHOLD_HPA} hPa/"
                    f"{RAIN_TREND_WINDOW_HOURS}hr]")
        elif trend > RAIN_TREND_RESET_HPA:
            rain_alert_active = False
            clear_pending("rain_pressure_drop")


# --- MQTT Callback ---
def on_message(client, userdata, message):
    global last_update_time

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
    elif topic == "home/weather/temperature":
        data_cache["weather_temp"] = payload
    elif topic == "home/weather/humidity":
        data_cache["weather_humi"] = payload
    elif topic == "home/weather/pressure":
        data_cache["weather_pres"] = payload
        try:
            record_pressure_reading(float(payload))
        except (ValueError, TypeError):
            pass

    last_update_time = datetime.datetime.now()

    evaluate_smart_rules()

    in_dew = calculate_dew_point(data_cache["indoor_temp"], data_cache["indoor_humi"])
    out_dew = calculate_dew_point(data_cache["outdoor_temp"], data_cache["outdoor_humi"])
    weather_dew = calculate_dew_point(data_cache["weather_temp"], data_cache["weather_humi"])

    lbl_in_temp.config(text=f"{data_cache['indoor_temp'] or '--.-'} °C")
    lbl_in_humi.config(text=f"Humidity: {data_cache['indoor_humi'] or '--'}%")
    lbl_in_dew.config(text=f"Dew Point: {in_dew if in_dew is not None else '--.-'} °C")

    lbl_out_temp.config(text=f"{data_cache['outdoor_temp'] or '--.-'} °C")
    lbl_out_humi.config(text=f"Humidity: {data_cache['outdoor_humi'] or '--'}%")
    lbl_out_dew.config(text=f"Dew Point: {out_dew if out_dew is not None else '--.-'} °C")

    lbl_weather_temp.config(text=f"{data_cache['weather_temp'] or '--.-'} °C")
    lbl_weather_humi.config(text=f"Humidity: {data_cache['weather_humi'] or '--'}%")
    lbl_weather_dew.config(text=f"Dew Point: {weather_dew if weather_dew is not None else '--.-'} °C")
    lbl_weather_pres.config(text=f"Pressure: {data_cache['weather_pres'] or '--.-'} hPa")


# --- Hourly Sensor Report ---
def build_sensor_report():
    """Builds the Telegram message containing the current sensor readings."""
    in_dew = calculate_dew_point(data_cache["indoor_temp"], data_cache["indoor_humi"])
    out_dew = calculate_dew_point(data_cache["outdoor_temp"], data_cache["outdoor_humi"])
    weather_dew = calculate_dew_point(data_cache["weather_temp"], data_cache["weather_humi"])

    def fmt(value, suffix=""):
        return f"{value}{suffix}" if value is not None else "--"

    stamp = datetime.datetime.now().strftime("%H:%M")

    return (
        f"📊 Hourly Sensor Report ({stamp})\n"
        f"\n"
        f"🏠 Indoor\n"
        f"   Temperature: {fmt(data_cache['indoor_temp'], ' °C')}\n"
        f"   Humidity: {fmt(data_cache['indoor_humi'], '%')}\n"
        f"   Dew Point: {fmt(in_dew, ' °C')}\n"
        f"\n"
        f"🌳 Outdoor\n"
        f"   Temperature: {fmt(data_cache['outdoor_temp'], ' °C')}\n"
        f"   Humidity: {fmt(data_cache['outdoor_humi'], '%')}\n"
        f"   Dew Point: {fmt(out_dew, ' °C')}\n"
        f"\n"
        f"🌦️ Weather\n"
        f"   Temperature: {fmt(data_cache['weather_temp'], ' °C')}\n"
        f"   Humidity: {fmt(data_cache['weather_humi'], '%')}\n"
        f"   Dew Point: {fmt(weather_dew, ' °C')}\n"
        f"   Pressure: {fmt(data_cache['weather_pres'], ' hPa')}"
    )


def send_hourly_report():
    """Fires on the hour. Sends readings only if the toggle is on and we're in the time window."""
    now = datetime.datetime.now()

    if hourly_reports_enabled and HOURLY_REPORT_START_HOUR <= now.hour <= HOURLY_REPORT_END_HOUR:
        send_telegram(build_sensor_report())

    schedule_next_hourly_report()


def schedule_next_hourly_report():
    """Calculates milliseconds until the next 00 minute mark."""
    now = datetime.datetime.now()
    target = (now + datetime.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    delay_ms = int((target - now).total_seconds() * 1000)
    root.after(max(delay_ms, 1000), send_hourly_report)


def apply_hourly_toggle(is_on):
    """Single place that changes hourly_reports_enabled, called from either the
    on-screen toggle switch (Tkinter thread) or the web dashboard (Flask thread).
    Keeps both UIs and the underlying flag in sync."""
    global hourly_reports_enabled
    hourly_reports_enabled = is_on

    if toggle_hourly.get() != is_on:
        toggle_hourly.set(is_on)  # will re-fire on_hourly_toggle, which is fine (idempotent)
        return

    if is_on:
        lbl_toggle.config(text="Hourly Report: ON", fg="#4ade80")
        print("Hourly Telegram reports ENABLED "
              f"({HOURLY_REPORT_START_HOUR:02d}:00 - {HOURLY_REPORT_END_HOUR:02d}:00)")
    else:
        lbl_toggle.config(text="Hourly Report: OFF", fg="#777777")
        print("Hourly Telegram reports DISABLED")


def on_hourly_toggle(is_on):
    """Called whenever the physical switch on the Pi's screen is tapped."""
    apply_hourly_toggle(is_on)


# --- Clock ---
def update_clock():
    lbl_clock.config(text=datetime.datetime.now().strftime("%a %d %b  %H:%M"))
    root.after(10000, update_clock)


# --- Rain Check (Fallback via Open-Meteo, since the BME280 can't sense rain itself) ---
def check_rain_status():
    """Runs periodically. Confirms *actual* rain via Open-Meteo, independent of
    the pressure-trend prediction above, and notifies on both the start and end
    of a rain event."""
    global currently_raining

    precip = get_current_precipitation()
    if precip is not None:
        if precip > 0 and not currently_raining:
            currently_raining = True
            send_telegram(
                f"🌧️ It's currently raining outside ({precip} mm in the last hour).\n\n"
                f"[Source: Open-Meteo current conditions - the BME280 can only infer a "
                f"pressure trend, not actual rainfall]")
        elif precip <= 0 and currently_raining:
            currently_raining = False
            send_telegram("☀️ Rain has stopped.")

    schedule_next_rain_check()


def schedule_next_rain_check():
    root.after(15 * 60 * 1000, check_rain_status)  # every 15 minutes


# --- Data Logging Functions ---
def init_log_file():
    """Creates the CSV file with headers if it doesn't exist."""
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, mode='w', newline='') as file:
            writer = csv.writer(file)
            writer.writerow([
                "Timestamp", "Indoor_Temp", "Indoor_Hum",
                "Outdoor_Temp", "Outdoor_Hum",
                "Weather_Temp", "Weather_Hum", "Weather_Pressure",
            ])


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
            data_cache["outdoor_humi"],
            data_cache["weather_temp"],
            data_cache["weather_humi"],
            data_cache["weather_pres"],
        ])

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


# ==========================================
# --- Web Dashboard (Flask) ---
# ==========================================
# Runs in its own background thread so it never blocks the Tkinter mainloop.
# It only ever *reads* data_cache / globals and writes through apply_hourly_toggle,
# so it stays safely decoupled from the Tkinter widgets themselves.

flask_app = Flask(__name__)


def build_web_state():
    in_dew = calculate_dew_point(data_cache["indoor_temp"], data_cache["indoor_humi"])
    out_dew = calculate_dew_point(data_cache["outdoor_temp"], data_cache["outdoor_humi"])
    weather_dew = calculate_dew_point(data_cache["weather_temp"], data_cache["weather_humi"])

    return {
        "clock": datetime.datetime.now().strftime("%a %d %b  %H:%M:%S"),
        "indoor": {
            "temp": data_cache["indoor_temp"],
            "humi": data_cache["indoor_humi"],
            "dew": in_dew,
        },
        "outdoor": {
            "temp": data_cache["outdoor_temp"],
            "humi": data_cache["outdoor_humi"],
            "dew": out_dew,
        },
        "weather": {
            "temp": data_cache["weather_temp"],
            "humi": data_cache["weather_humi"],
            "dew": weather_dew,
            "pressure": data_cache["weather_pres"],
        },
        "advice": latest_advice_display,
        "hourly_enabled": hourly_reports_enabled,
        "last_update": last_update_time.strftime("%H:%M:%S") if last_update_time else None,
    }


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Smart Home Hub</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: #121212;
    color: #eee;
    font-family: -apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif;
    padding: 16px;
  }
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 18px;
  }
  #clock {
    font-size: 13px;
    font-weight: bold;
    color: #666;
  }
  #last-update {
    font-size: 11px;
    color: #555;
    margin-top: 2px;
  }
  .toggle-wrap {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  #toggle-label {
    font-size: 13px;
    font-weight: bold;
    color: #777;
  }
  .switch {
    position: relative;
    display: inline-block;
    width: 56px;
    height: 30px;
  }
  .switch input { opacity: 0; width: 0; height: 0; }
  .slider {
    position: absolute;
    cursor: pointer;
    inset: 0;
    background-color: #4a4a4a;
    transition: .2s;
    border-radius: 30px;
  }
  .slider:before {
    position: absolute;
    content: "";
    height: 22px;
    width: 22px;
    left: 4px;
    bottom: 4px;
    background-color: white;
    transition: .2s;
    border-radius: 50%;
  }
  input:checked + .slider { background-color: #4ade80; }
  input:checked + .slider:before { transform: translateX(26px); }

  .panels {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 14px;
    margin-bottom: 14px;
  }
  @media (max-width: 700px) {
    .panels { grid-template-columns: 1fr; }
  }
  .panel {
    background: #1a1a1a;
    border: 2px solid #2a2a2a;
    border-radius: 10px;
    padding: 16px;
    text-align: center;
  }
  .panel h2 {
    margin: 0 0 8px 0;
    font-size: 13px;
    letter-spacing: 0.5px;
  }
  .panel .temp {
    font-size: 30px;
    font-weight: bold;
    margin: 6px 0;
  }
  .panel .sub {
    font-size: 12px;
    color: #aaa;
    margin: 2px 0;
  }
  .panel .dew {
    font-size: 12px;
    font-style: italic;
    margin-top: 6px;
  }
  #indoor h2 { color: #3498db; }
  #outdoor h2 { color: #2ecc71; }
  #weather h2 { color: #f4b942; }
  #indoor .dew { color: #85c1e9; }
  #outdoor .dew { color: #a3e4d7; }
  #weather .dew { color: #f5cf87; }

  #advice {
    border: 2px solid #2a2a2a;
    border-radius: 10px;
    padding: 22px;
    text-align: center;
    font-weight: bold;
    font-size: 16px;
    white-space: pre-line;
    background: #1f1f1f;
    color: #888;
    transition: background-color .3s, color .3s;
  }
</style>
</head>
<body>

<header>
  <div>
    <div id="clock">--</div>
    <div id="last-update"></div>
  </div>
  <div class="toggle-wrap">
    <span id="toggle-label">Hourly Report: OFF</span>
    <label class="switch">
      <input type="checkbox" id="toggle-input">
      <span class="slider"></span>
    </label>
  </div>
</header>

<div class="panels">
  <div class="panel" id="indoor">
    <h2>INDOOR</h2>
    <div class="temp" id="indoor-temp">--.- °C</div>
    <div class="sub" id="indoor-humi">Humidity: --%</div>
    <div class="dew" id="indoor-dew">Dew Point: --.- °C</div>
  </div>
  <div class="panel" id="outdoor">
    <h2>OUTDOOR</h2>
    <div class="temp" id="outdoor-temp">--.- °C</div>
    <div class="sub" id="outdoor-humi">Humidity: --%</div>
    <div class="dew" id="outdoor-dew">Dew Point: --.- °C</div>
  </div>
  <div class="panel" id="weather">
    <h2>WEATHER</h2>
    <div class="temp" id="weather-temp">--.- °C</div>
    <div class="sub" id="weather-humi">Humidity: --%</div>
    <div class="dew" id="weather-dew">Dew Point: --.- °C</div>
    <div class="sub" id="weather-pres">Pressure: --.- hPa</div>
  </div>
</div>

<div id="advice">Awaiting confirmed readings...</div>

<script>
const fmt = (v, unit) => (v === null || v === undefined) ? "--" + unit : v + unit;

let togglingFromServer = false;

async function refresh() {
  try {
    const res = await fetch('/api/state');
    const s = await res.json();

    document.getElementById('clock').textContent = s.clock;
    document.getElementById('last-update').textContent =
      s.last_update ? ('Last reading: ' + s.last_update) : 'No readings yet';

    document.getElementById('indoor-temp').textContent = fmt(s.indoor.temp, ' °C');
    document.getElementById('indoor-humi').textContent = 'Humidity: ' + fmt(s.indoor.humi, '%');
    document.getElementById('indoor-dew').textContent = 'Dew Point: ' + fmt(s.indoor.dew, ' °C');

    document.getElementById('outdoor-temp').textContent = fmt(s.outdoor.temp, ' °C');
    document.getElementById('outdoor-humi').textContent = 'Humidity: ' + fmt(s.outdoor.humi, '%');
    document.getElementById('outdoor-dew').textContent = 'Dew Point: ' + fmt(s.outdoor.dew, ' °C');

    document.getElementById('weather-temp').textContent = fmt(s.weather.temp, ' °C');
    document.getElementById('weather-humi').textContent = 'Humidity: ' + fmt(s.weather.humi, '%');
    document.getElementById('weather-dew').textContent = 'Dew Point: ' + fmt(s.weather.dew, ' °C');
    document.getElementById('weather-pres').textContent = 'Pressure: ' + fmt(s.weather.pressure, ' hPa');

    const advice = document.getElementById('advice');
    advice.textContent = s.advice.text;
    advice.style.color = s.advice.fg;
    advice.style.backgroundColor = s.advice.bg;

    const toggleInput = document.getElementById('toggle-input');
    if (!togglingFromServer) {
      toggleInput.checked = s.hourly_enabled;
    }
    document.getElementById('toggle-label').textContent =
      'Hourly Report: ' + (s.hourly_enabled ? 'ON' : 'OFF');
    document.getElementById('toggle-label').style.color = s.hourly_enabled ? '#4ade80' : '#777';
  } catch (e) {
    console.error('Failed to refresh dashboard state', e);
  }
}

document.getElementById('toggle-input').addEventListener('change', async (e) => {
  togglingFromServer = true;
  try {
    await fetch('/api/toggle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: e.target.checked })
    });
  } catch (err) {
    console.error('Failed to toggle hourly report', err);
  } finally {
    togglingFromServer = false;
    refresh();
  }
});

refresh();
setInterval(refresh, 4000);
</script>

</body>
</html>
"""


@flask_app.route("/")
def dashboard():
    return Response(DASHBOARD_HTML, mimetype="text/html")


@flask_app.route("/api/state")
def api_state():
    return jsonify(build_web_state())


@flask_app.route("/api/toggle", methods=["POST"])
def api_toggle():
    payload = request.get_json(silent=True) or {}
    is_on = bool(payload.get("enabled"))
    # Tkinter widget updates happen on whichever thread calls this - the existing
    # MQTT callback already does the same thing, so this is consistent with how
    # the rest of the app already touches the UI from a background thread.
    apply_hourly_toggle(is_on)
    return jsonify({"enabled": hourly_reports_enabled})


def start_web_server():
    flask_app.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False)


# --- UI Setup ---
root = tk.Tk()
root.title("Smart Home Hub")
root.configure(bg="#121212")
root.attributes('-fullscreen', True)

# --- Header Bar (clock on the left, toggle switch on the right) ---
frame_header = tk.Frame(root, bg="#121212")
frame_header.place(relx=0.04, rely=0.015, relwidth=0.92, relheight=0.13)

lbl_clock = tk.Label(frame_header, text="", font=("Helvetica", 10, "bold"),
                     fg="#666666", bg="#121212")
lbl_clock.pack(side="left", pady=4)

toggle_hourly = ToggleSwitch(frame_header, width=76, height=36, bg="#121212",
                             command=on_hourly_toggle, initial=False)
toggle_hourly.pack(side="right", padx=(8, 0))

lbl_toggle = tk.Label(frame_header, text="Hourly Report: OFF",
                      font=("Helvetica", 10, "bold"), fg="#777777", bg="#121212")
lbl_toggle.pack(side="right")

# Tapping the label toggles too, giving a bigger touch target on the 3.5" screen
lbl_toggle.bind("<Button-1>", toggle_hourly.toggle)

# --- Sensor Panels (Indoor / Outdoor / Weather, side by side) ---
frame_indoor = tk.Frame(root, bg="#1a1a1a", bd=2, relief="groove")
frame_indoor.place(relx=0.02, rely=0.16, relwidth=0.30, relheight=0.58)

tk.Label(frame_indoor, text="INDOOR", font=("Helvetica", 12, "bold"), fg="#3498db", bg="#1a1a1a").pack(pady=4)
lbl_in_temp = tk.Label(frame_indoor, text="--.- °C", font=("Helvetica", 18, "bold"), fg="white", bg="#1a1a1a")
lbl_in_temp.pack(pady=4)
lbl_in_humi = tk.Label(frame_indoor, text="Humidity: --%", font=("Helvetica", 10), fg="#aaaaaa", bg="#1a1a1a")
lbl_in_humi.pack()
lbl_in_dew = tk.Label(frame_indoor, text="Dew Point: --.- °C", font=("Helvetica", 10, "italic"), fg="#85c1e9",
                      bg="#1a1a1a")
lbl_in_dew.pack(pady=4)

frame_outdoor = tk.Frame(root, bg="#1a1a1a", bd=2, relief="groove")
frame_outdoor.place(relx=0.35, rely=0.16, relwidth=0.30, relheight=0.58)

tk.Label(frame_outdoor, text="OUTDOOR", font=("Helvetica", 12, "bold"), fg="#2ecc71", bg="#1a1a1a").pack(pady=4)
lbl_out_temp = tk.Label(frame_outdoor, text="--.- °C", font=("Helvetica", 18, "bold"), fg="white", bg="#1a1a1a")
lbl_out_temp.pack(pady=4)
lbl_out_humi = tk.Label(frame_outdoor, text="Humidity: --%", font=("Helvetica", 10), fg="#aaaaaa", bg="#1a1a1a")
lbl_out_humi.pack()
lbl_out_dew = tk.Label(frame_outdoor, text="Dew Point: --.- °C", font=("Helvetica", 10, "italic"), fg="#a3e4d7",
                       bg="#1a1a1a")
lbl_out_dew.pack(pady=4)

frame_weather = tk.Frame(root, bg="#1a1a1a", bd=2, relief="groove")
frame_weather.place(relx=0.68, rely=0.16, relwidth=0.30, relheight=0.58)

tk.Label(frame_weather, text="WEATHER", font=("Helvetica", 12, "bold"), fg="#f4b942", bg="#1a1a1a").pack(pady=4)
lbl_weather_temp = tk.Label(frame_weather, text="--.- °C", font=("Helvetica", 18, "bold"), fg="white", bg="#1a1a1a")
lbl_weather_temp.pack(pady=4)
lbl_weather_humi = tk.Label(frame_weather, text="Humidity: --%", font=("Helvetica", 10), fg="#aaaaaa", bg="#1a1a1a")
lbl_weather_humi.pack()
lbl_weather_dew = tk.Label(frame_weather, text="Dew Point: --.- °C", font=("Helvetica", 10, "italic"), fg="#f5cf87",
                           bg="#1a1a1a")
lbl_weather_dew.pack(pady=2)
lbl_weather_pres = tk.Label(frame_weather, text="Pressure: --.- hPa", font=("Helvetica", 10), fg="#cccccc",
                            bg="#1a1a1a")
lbl_weather_pres.pack(pady=2)

frame_advice = tk.Frame(root, bg="#1f1f1f", bd=2, relief="groove")
frame_advice.place(relx=0.04, rely=0.77, relwidth=0.92, relheight=0.19)

lbl_advice = tk.Label(frame_advice, text="Awaiting sensor readings...", font=("Helvetica", 11, "bold"), fg="#888888",
                      bg="#1f1f1f")
lbl_advice.pack(expand=True, fill="both")

root.bind("<Escape>", lambda e: root.destroy())

# --- Initialization & Background Setup ---
init_log_file()
schedule_next_log()
schedule_next_hourly_report()
schedule_next_rain_check()
update_clock()

# Web dashboard runs in a daemon thread so it dies automatically when the
# Tkinter app (and thus the whole process) exits.
web_thread = threading.Thread(target=start_web_server, daemon=True)
web_thread.start()
print(f"Web dashboard available on the LAN at http://192.168.1.132:{WEB_PORT}")

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_message = on_message
client.connect("localhost", 1883)
client.subscribe("home/+/temperature")
client.subscribe("home/+/humidity")
client.subscribe("home/+/pressure")
client.loop_start()

root.mainloop()