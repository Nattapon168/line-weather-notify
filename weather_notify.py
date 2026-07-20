#!/usr/bin/env python3
"""
weather_notify.py
ดึงข้อมูลสภาพอากาศ + PM2.5 แล้วส่งสรุปเข้า LINE กลุ่ม (บอทที่ 2)

แหล่งข้อมูล:
- อุณหภูมิ / พยากรณ์ล่วงหน้า / ปริมาณฝน / เวลาพระอาทิตย์ขึ้น-ตก : Open-Meteo (ไม่ต้องใช้ API key)
- PM2.5 / AQI : Air4Thai - กรมควบคุมมลพิษ (ไม่ต้องใช้ API key)
- ส่งเข้า LINE : LINE Messaging API (push message) ใช้ Channel Access Token ของ OA ตัวที่ 2

วิธีใช้:
1. เชิญ LINE OA ตัวที่ 2 เข้ากลุ่มที่ต้องการ
2. รัน get_line_group_id.py (ตัวเดิม) + ngrok เพื่อดัก Group ID ของกลุ่มนี้ (ตามขั้นตอนเดิมที่เคยทำ)
3. เอา CHANNEL_ACCESS_TOKEN (จาก LINE Developers Console ของ OA ตัวที่ 2) และ GROUP_ID มาใส่ด้านล่าง
4. ตั้งพิกัด LATITUDE/LONGITUDE ให้ตรงพื้นที่ที่ต้องการ (ค่า default = เชียงใหม่)
5. รันด้วยมือก่อนเพื่อทดสอบ: python3 weather_notify.py
6. ถ้าโอเค ค่อยตั้ง cron ให้รันอัตโนมัติทุกเช้า (ดูตัวอย่าง crontab ท้ายไฟล์)
"""

import os
import sys
import requests
import urllib3
from datetime import datetime, timedelta
from math import radians, sin, cos, sqrt, atan2

# ปิด warning ที่เกิดจากการปิด SSL verification เฉพาะจุด (ดู get_pm25() ด้านล่าง)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ========== CONFIG ==========
# อ่านค่าจาก environment variable ก่อน (ใช้กับ GitHub Actions Secrets ได้เลย)
# ถ้าจะรันบนเครื่องตัวเอง ใส่ค่าตรงๆ แทน os.environ.get(...) ก็ได้
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_GROUP_ID = os.environ.get("LINE_GROUP_ID", "")

# พิกัดพื้นที่ที่ต้องการพยากรณ์ (ค่า default: เชียงใหม่)
LATITUDE = float(os.environ.get("WEATHER_LAT", 18.8488))
LONGITUDE = float(os.environ.get("WEATHER_LON", 99.0446))
LOCATION_NAME = os.environ.get("WEATHER_LOCATION_NAME", "อำเภอสันทราย จ.เชียงใหม่ 50210")

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_GROUP_ID:
    print("[ERROR] ไม่พบ LINE_CHANNEL_ACCESS_TOKEN หรือ LINE_GROUP_ID")
    print("ตั้งเป็น environment variable หรือ GitHub Secrets ก่อนรัน")
    sys.exit(1)
# ========================================


def get_weather():
    """ดึงพยากรณ์อากาศจาก Open-Meteo (วันนี้ + พรุ่งนี้)"""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "current": "temperature_2m,precipitation,cloud_cover",
        "hourly": "cloud_cover,precipitation_probability",
        "daily": (
            "temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max,precipitation_sum,"
            "sunrise,sunset,uv_index_max"
        ),
        "timezone": "Asia/Bangkok",
        "forecast_days": 2,
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def estimate_sun_hours(hourly_data, target_date_str):
    """
    ประมาณช่วงเวลาที่ 'แดดออก' (ท้องฟ้าโปร่ง cloud_cover < 40%) ในวันที่กำหนด
    โดยดูจาก hourly cloud_cover ระหว่าง 06:00-18:00
    คืนค่าเป็น list ของช่วงเวลา เช่น ["09:00-12:00", "14:00-16:00"]
    """
    times = hourly_data["time"]
    clouds = hourly_data["cloud_cover"]

    sunny_hours = []
    for t, c in zip(times, clouds):
        if t.startswith(target_date_str):
            hour = int(t.split("T")[1].split(":")[0])
            if 6 <= hour <= 18 and c is not None and c < 40:
                sunny_hours.append(hour)

    if not sunny_hours:
        return ["ไม่มีช่วงแดดจัดชัดเจน (มีเมฆมากตลอดวัน)"]

    # รวมชั่วโมงติดกันเป็นช่วง
    ranges = []
    start = prev = sunny_hours[0]
    for h in sunny_hours[1:]:
        if h == prev + 1:
            prev = h
        else:
            ranges.append((start, prev))
            start = prev = h
    ranges.append((start, prev))

    return [f"{s:02d}:00-{e+1:02d}:00" for s, e in ranges]


def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def get_pm25():
    """ดึงค่า PM2.5 จาก Air4Thai แล้วหาสถานีที่ใกล้พิกัดที่สุด"""
    url = "http://air4thai.pcd.go.th/services/getNewAQI_JSON.php"
    # หมายเหตุ: เว็บ Air4Thai มีปัญหาใบรับรอง SSL ของตัวเอง (ไม่ใช่ข้อมูลอ่อนไหว
    # จึงปิดการตรวจสอบใบรับรองเฉพาะจุดนี้เพื่อให้ดึงข้อมูลได้)
    r = requests.get(url, timeout=15, verify=False)
    r.raise_for_status()
    data = r.json()

    nearest = None
    nearest_dist = float("inf")
    for station in data.get("stations", []):
        try:
            lat = float(station["lat"])
            lon = float(station["long"])
        except (KeyError, ValueError, TypeError):
            continue
        dist = haversine(LATITUDE, LONGITUDE, lat, lon)
        if dist < nearest_dist:
            nearest_dist = dist
            nearest = station

    if not nearest:
        return None

    last = nearest.get("LastUpdate", {})
    pm25 = last.get("PM25", {}).get("value", "n/a")
    aqi = last.get("AQI", {}).get("aqi", "n/a")
    return {
        "station_name": nearest.get("nameTH", "ไม่ทราบชื่อสถานี"),
        "distance_km": round(nearest_dist, 1),
        "pm25": pm25,
        "aqi": aqi,
    }


def aqi_level_text(pm25_value):
    try:
        v = float(pm25_value)
    except (TypeError, ValueError):
        return ""
    if v <= 25:
        return "ดีมาก 🔵"
    elif v <= 50:
        return "ดี 🟢"
    elif v <= 100:
        return "ปานกลาง 🟡"
    elif v <= 200:
        return "เริ่มมีผลต่อสุขภาพ 🟠"
    else:
        return "อันตราย 🔴"


def build_message():
    weather = get_weather()
    today = weather["daily"]
    hourly = weather["hourly"]
    current = weather["current"]

    today_str = today["time"][0]
    tomorrow_str = today["time"][1]

    sun_ranges_today = estimate_sun_hours(hourly, today_str)

    try:
        pm = get_pm25()
    except Exception as e:
        print(f"[WARN] ดึงข้อมูล PM2.5 ไม่สำเร็จ ({e}) จะข้ามส่วนนี้ไป")
        pm = None

    lines = []
    lines.append(f"🌤️ สรุปสภาพอากาศ - {LOCATION_NAME}")
    lines.append(f"📅 {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    lines.append("")
    lines.append("── วันนี้ ──")
    lines.append(f"🌡️ อุณหภูมิปัจจุบัน: {current['temperature_2m']}°C")
    lines.append(f"🌡️ สูงสุด/ต่ำสุด: {today['temperature_2m_max'][0]}°C / {today['temperature_2m_min'][0]}°C")
    lines.append(f"🌧️ โอกาสฝนตก: {today['precipitation_probability_max'][0]}%  (ปริมาณ ~{today['precipitation_sum'][0]} มม.)")
    lines.append(f"☀️ ช่วงแดดออก (เมฆน้อย): {', '.join(sun_ranges_today)}")
    lines.append(f"🌅 พระอาทิตย์ขึ้น: {today['sunrise'][0].split('T')[1]}  🌇 ตก: {today['sunset'][0].split('T')[1]}")

    if pm:
        lines.append("")
        lines.append("── คุณภาพอากาศ (PM2.5) ──")
        lines.append(f"😷 PM2.5: {pm['pm25']} µg/m³  ({aqi_level_text(pm['pm25'])})")
        lines.append(f"📊 AQI: {pm['aqi']}")
        lines.append(f"📍 สถานีที่ใกล้ที่สุด: {pm['station_name']} (~{pm['distance_km']} กม.)")

    lines.append("")
    lines.append("── พรุ่งนี้ ──")
    lines.append(f"🌡️ สูงสุด/ต่ำสุด: {today['temperature_2m_max'][1]}°C / {today['temperature_2m_min'][1]}°C")
    lines.append(f"🌧️ โอกาสฝนตก: {today['precipitation_probability_max'][1]}%  (ปริมาณ ~{today['precipitation_sum'][1]} มม.)")

    lines.append("")
    lines.append("ข้อมูล: Open-Meteo / Air4Thai (กรมควบคุมมลพิษ)")

    return "\n".join(lines)


def push_line_message(text):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    payload = {
        "to": LINE_GROUP_ID,
        "messages": [{"type": "text", "text": text}],
    }
    r = requests.post(url, headers=headers, json=payload, timeout=15)
    if r.status_code != 200:
        print(f"[ERROR] LINE push ล้มเหลว: {r.status_code} {r.text}")
    else:
        print("[OK] ส่งข้อความสำเร็จ")


if __name__ == "__main__":
    msg = build_message()
    print(msg)
    print("-" * 40)
    push_line_message(msg)

# ========== การตั้งเวลารันอัตโนมัติ ==========
# แนะนำ: ใช้ GitHub Actions (ไม่ต้องมีเซิร์ฟเวอร์ของตัวเอง)
# ดูไฟล์ .github/workflows/weather.yml ที่มาคู่กัน
#
# หรือถ้ามีเครื่อง/เซิร์ฟเวอร์จริงๆ จะใช้ cron แบบเดิมก็ได้:
# 0 6 * * * LINE_CHANNEL_ACCESS_TOKEN=xxx LINE_GROUP_ID=xxx /usr/bin/python3 /home/user/weather_notify.py >> /home/user/weather_notify.log 2>&1
# ================================================
