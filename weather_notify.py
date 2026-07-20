#!/usr/bin/env python3
"""
weather_notify.py
ดึงข้อมูลสภาพอากาศ + PM2.5 + ผลสลากกินแบ่งรัฐบาล แล้วส่งสรุปเข้า LINE กลุ่ม (บอทที่ 2)

แหล่งข้อมูล:
- อุณหภูมิ / พยากรณ์ล่วงหน้า / ปริมาณฝน / เวลาพระอาทิตย์ขึ้น-ตก : Open-Meteo (ไม่ต้องใช้ API key)
- PM2.5 / AQI : Air4Thai - กรมควบคุมมลพิษ (ไม่ต้องใช้ API key)
- ผลสลากกินแบ่งรัฐบาล : GLO - สำนักงานสลากกินแบ่งรัฐบาล (ไม่ต้องใช้ API key)
- ส่งเข้า LINE : LINE Messaging API (push message) ใช้ Channel Access Token ของ OA ตัวที่ 2

วิธีใช้:
1. เชิญ LINE OA ตัวที่ 2 เข้ากลุ่มที่ต้องการ
2. รัน get_line_group_id.py (ตัวเดิม) + ngrok เพื่อดัก Group ID ของกลุ่มนี้ (ตามขั้นตอนเดิมที่เคยทำ)
3. เอา CHANNEL_ACCESS_TOKEN (จาก LINE Developers Console ของ OA ตัวที่ 2) และ GROUP_ID มาใส่ด้านล่าง
4. ตั้งพิกัด LATITUDE/LONGITUDE ให้ตรงพื้นที่ที่ต้องการ (ค่า default = เชียงใหม่)
5. รันด้วยมือก่อนเพื่อทดสอบ: python3 weather_notify.py
6. ถ้าโอเค ค่อยตั้ง cron / GitHub Actions ให้รันอัตโนมัติทุกเช้า (ดูตัวอย่างท้ายไฟล์)

หมายเหตุเกี่ยวกับสลากกินแบ่งรัฐบาล (GLO API):
- API ของ GLO เป็น endpoint ที่ไม่มีเอกสารทางการแบบละเอียด (พบจาก Data Catalog ของ GLO เอง)
  โครงสร้าง JSON ที่ตอบกลับมาอาจเปลี่ยนแปลงได้โดยไม่แจ้งล่วงหน้า
- โค้ดด้านล่างพยายาม parse ด้วยหลาย path ป้องกัน error ไว้แล้ว แต่ถ้ารันแล้วได้ "โครงสร้างข้อมูลไม่ตรงตามที่คาด"
  ให้ดูใน log ที่ print raw JSON ออกมา แล้วส่งกลับมาให้ปรับโค้ดเพิ่มได้
"""

import os
import sys
import json
import random
import hashlib
import requests
import urllib3
from datetime import datetime, timedelta, date
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

# เปิด/ปิดส่วนสลากกินแบ่งรัฐบาล (ตั้งเป็น "0" ถ้าไม่ต้องการให้ดึงส่วนนี้)
ENABLE_LOTTERY = os.environ.get("ENABLE_LOTTERY", "1") != "0"

# เปิด/ปิดส่วนเลขสุ่มเสี่ยงดวง (เอาฮาอย่างเดียว ไม่เกี่ยวกับผลจริง)
ENABLE_LUCKY_NUMBER = os.environ.get("ENABLE_LUCKY_NUMBER", "1") != "0"

# เปิด/ปิดส่วนราคาทอง/น้ำมัน/ค่าเงิน (แสดงคู่กับพยากรณ์อากาศทุกวัน)
ENABLE_MARKET_PRICES = os.environ.get("ENABLE_MARKET_PRICES", "1") != "0"

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
    """
    ดึงค่า PM2.5 จาก Air4Thai แล้วหาสถานีที่ 'ใกล้ที่สุดและมีค่า PM2.5 จริง'

    [แก้ไข] ของเดิมจะเลือกสถานีที่ใกล้ที่สุดโดยไม่สนใจว่ามีค่า PM2.5 จริงหรือไม่
    ทำให้ถ้าสถานีที่ใกล้ที่สุดไม่มีเซ็นเซอร์ PM2.5 (มีแค่ก๊าซอื่น) หรือเครื่องไม่อัปเดต
    จะได้ค่า "n/a" ทุกครั้ง ตอนนี้แก้เป็น: ไล่หาในบรรดาสถานีทั้งหมด แล้วเลือกสถานี
    ที่ใกล้ที่สุด "ในกลุ่มที่มีค่า PM2.5 เป็นตัวเลขจริง" เท่านั้น
    """
    url = "http://air4thai.pcd.go.th/services/getNewAQI_JSON.php"
    # หมายเหตุ: เว็บ Air4Thai มีปัญหาใบรับรอง SSL ของตัวเอง (ไม่ใช่ข้อมูลอ่อนไหว
    # จึงปิดการตรวจสอบใบรับรองเฉพาะจุดนี้เพื่อให้ดึงข้อมูลได้)
    r = requests.get(url, timeout=15, verify=False)
    r.raise_for_status()
    data = r.json()

    candidates = []
    for station in data.get("stations", []):
        try:
            lat = float(station["lat"])
            lon = float(station["long"])
        except (KeyError, ValueError, TypeError):
            continue

        # [แก้ไข] key จริงคือ "AQILast" ไม่ใช่ "LastUpdate" (ยืนยันจาก raw data จริง)
        last = station.get("AQILast", {}) or {}
        pm25_info = last.get("PM25", {}) or {}
        pm25_val = pm25_info.get("value", "n/a")

        # ข้ามสถานีที่ไม่มีค่า PM2.5 จริง (ไม่มีเซ็นเซอร์ / เครื่องเสีย / ยังไม่อัปเดตวันนี้)
        try:
            float(pm25_val)
        except (TypeError, ValueError):
            continue

        dist = haversine(LATITUDE, LONGITUDE, lat, lon)
        candidates.append((dist, station, last, pm25_info))

    if not candidates:
        # [DEBUG] ไม่พบสถานีที่มีค่า PM2.5 เป็นตัวเลขเลย -> dump raw JSON ของสถานี
        # ที่ใกล้ที่สุด 2 แห่งออกมาทั้งก้อน เพื่อดูโครงสร้างจริงว่า key ชื่ออะไรกันแน่
        print("[DEBUG] ไม่พบสถานีที่มีค่า PM2.5 เป็นตัวเลข raw data สถานีใกล้ที่สุด 2 แห่ง:")
        all_dist = []
        for station in data.get("stations", []):
            try:
                lat = float(station["lat"])
                lon = float(station["long"])
            except (KeyError, ValueError, TypeError):
                continue
            dist = haversine(LATITUDE, LONGITUDE, lat, lon)
            all_dist.append((dist, station))
        all_dist.sort(key=lambda x: x[0])
        for dist, station in all_dist[:2]:
            print(f"    ~{dist:.1f} กม. -> {json.dumps(station, ensure_ascii=False)}")
        return None

    candidates.sort(key=lambda x: x[0])
    dist, nearest, last, pm25_info = candidates[0]

    aqi = last.get("AQI", {}).get("aqi", "n/a")
    return {
        "station_name": nearest.get("nameTH", "ไม่ทราบชื่อสถานี"),
        "distance_km": round(dist, 1),
        "pm25": pm25_info.get("value", "n/a"),
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


# ========== ส่วนสลากกินแบ่งรัฐบาล (GLO) ==========

GLO_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}


def _extract_numbers(section):
    """ดึง list ของเลขจาก section รูปแบบ {"number": [{"value": "639214"}, ...]}"""
    if not section:
        return []
    nums = section.get("number", [])
    return [n.get("value", "") for n in nums if isinstance(n, dict) and n.get("value")]


THAI_MONTHS = [
    "", "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
    "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม",
]


def _extract_date_parts(display_date_raw, fallback_iso_date=None):
    """คืนค่า (day, month, year_ce) เป็น int หรือ (None, None, None) ถ้าแปลงไม่ได้"""
    day = month = year_ce = None

    if isinstance(display_date_raw, dict):
        day = display_date_raw.get("date")
        month = display_date_raw.get("month")
        year_ce = display_date_raw.get("year")
    elif isinstance(display_date_raw, str) and "-" in display_date_raw:
        parts = display_date_raw.split("-")
        if len(parts) == 3:
            year_ce, month, day = parts

    if day is None and fallback_iso_date and "-" in str(fallback_iso_date):
        parts = str(fallback_iso_date).split("-")
        if len(parts) == 3:
            year_ce, month, day = parts

    try:
        return int(day), int(month), int(year_ce)
    except (TypeError, ValueError):
        return None, None, None


def _format_thai_date(day, month, year_ce):
    try:
        return f"{day} {THAI_MONTHS[month]} {year_ce + 543}"
    except (TypeError, IndexError):
        return "ไม่ทราบวันที่"


def _parse_lottery_payload(payload):
    """
    ดึงข้อมูลจาก JSON ที่ GLO ส่งกลับมา
    โครงสร้างจริง (ยืนยันจาก log): payload["response"]["data"]["first"/"last2"/...]
    และ payload["response"]["displayDate"] เป็น dict {"date","month","year"} (ปี ค.ศ.)
    ใส่ fallback หลาย path ไว้เผื่อ GLO เปลี่ยนโครงสร้างในอนาคต
    """
    resp = payload
    if isinstance(payload, dict) and isinstance(payload.get("response"), dict):
        resp = payload["response"]

    data = None
    for get_data in (
        lambda p: p["data"],
        lambda p: p["result"]["data"],
        lambda p: p,
    ):
        try:
            candidate = get_data(resp)
            if isinstance(candidate, dict) and "first" in candidate:
                data = candidate
                break
        except (KeyError, TypeError):
            continue

    if data is None:
        print("[WARN] โครงสร้าง JSON ของ GLO ไม่ตรงตามที่คาด raw payload ด้านล่างนี้:")
        print(payload)
        return None

    display_date_raw = resp.get("displayDate") if isinstance(resp, dict) else None
    fallback_iso_date = resp.get("date") if isinstance(resp, dict) else None
    day, month, year_ce = _extract_date_parts(display_date_raw, fallback_iso_date)

    date_obj = None
    if day and month and year_ce:
        try:
            date_obj = datetime(year_ce, month, day).date()
        except ValueError:
            date_obj = None

    display_date = _format_thai_date(day, month, year_ce) if date_obj else "ไม่ทราบวันที่"

    return {
        "display_date": display_date,
        "date_obj": date_obj,
        "first": _extract_numbers(data.get("first")),
        "last2": _extract_numbers(data.get("last2")),
        "last3f": _extract_numbers(data.get("last3f")),
        "last3b": _extract_numbers(data.get("last3b")),
    }


def get_latest_lottery():
    """ดึงผลสลากกินแบ่งรัฐบาล งวดล่าสุด"""
    url = "https://www.glo.or.th/api/lottery/getLatestLottery"
    r = requests.post(url, headers=GLO_HEADERS, timeout=15)
    r.raise_for_status()
    return _parse_lottery_payload(r.json())


def get_lottery_by_date(date_obj):
    """ดึงผลสลากกินแบ่งรัฐบาลของงวดวันที่ที่ระบุ (date_obj ต้องเป็นวันที่ 1 หรือ 16 ของเดือน)"""
    url = "https://www.glo.or.th/api/checking/getLotteryResult"
    payload = {
        "date": f"{date_obj.day:02d}",
        "month": f"{date_obj.month:02d}",
        "year": str(date_obj.year),
    }
    r = requests.post(url, headers=GLO_HEADERS, json=payload, timeout=15)
    r.raise_for_status()
    return _parse_lottery_payload(r.json())


def previous_period_date(d):
    """คืนวันที่ (datetime.date) ของงวดก่อนหน้า โดย d ต้องเป็นวันที่ 1 หรือ 16"""
    if d.day == 16:
        return d.replace(day=1)
    else:
        last_day_prev_month = d.replace(day=1) - timedelta(days=1)
        return last_day_prev_month.replace(day=16)


def format_lottery_block(result, heading):
    lines = [heading]
    lines.append(f"🥇 รางวัลที่ 1: {', '.join(result['first']) or '-'}")
    if result["last2"]:
        lines.append(f"🔚 เลขท้าย 2 ตัว: {', '.join(result['last2'])}")
    if result["last3b"]:
        lines.append(f"🔚 เลขท้าย 3 ตัว: {', '.join(result['last3b'])}")
    if result["last3f"]:
        lines.append(f"🔜 เลขหน้า 3 ตัว: {', '.join(result['last3f'])}")
    return lines


def build_lottery_section():
    """
    สร้างข้อความส่วนผลสลากกินแบ่งรัฐบาล
    - แสดง "เฉพาะวันที่มีการออกสลากงวดใหม่แล้วจริงๆ" เท่านั้น (เช็คว่าวันที่ของงวดล่าสุด
      ที่ GLO ประกาศ ตรงกับวันนี้หรือไม่ ไม่ใช่แค่เช็คว่าวันนี้เป็นวันที่ 1 หรือ 16 เฉยๆ
      เพราะเช้าของวันที่ 1/16 ผลอาจยังไม่ประกาศออกมา)
    - ถ้าใช่ (มีงวดใหม่ออกวันนี้) จะแสดงทั้งงวดใหม่และงวดก่อนหน้าคู่กัน
    - ถ้าวันนี้ไม่ใช่วันออกสลาก (หรืองวดใหม่ยังไม่ประกาศ) จะคืนค่า None (ไม่แสดงส่วนนี้เลย)
    """
    today = datetime.now().date()

    try:
        latest = get_latest_lottery()
    except Exception as e:
        print(f"[WARN] ดึงผลสลากงวดล่าสุดไม่สำเร็จ ({e})")
        return None

    if not latest:
        return None

    if latest.get("date_obj") != today:
        # วันนี้ไม่ใช่วันที่มีงวดใหม่ออก (หรือ GLO ยังไม่ประกาศผลของวันนี้) -> ไม่ต้องแสดง
        return None

    lines = ["", "── สลากกินแบ่งรัฐบาล (งวดใหม่วันนี้) ──"]
    lines += format_lottery_block(latest, f"🎟️ งวดประจำวันที่ {latest['display_date']}")

    prev_date = previous_period_date(today)
    try:
        prev = get_lottery_by_date(prev_date)
    except Exception as e:
        print(f"[WARN] ดึงผลสลากงวดก่อนหน้าไม่สำเร็จ ({e})")
        prev = None

    if prev:
        lines.append("")
        lines += format_lottery_block(prev, f"📌 งวดก่อนหน้า ({prev['display_date']}):")

    return "\n".join(lines)


# ========== ส่วนเลขสุ่มเสี่ยงดวง (เอาฮาอย่างเดียว) ==========


def next_draw_date(today):
    """คืนวันที่ (datetime.date) ของงวดสลากที่จะออกถัดไป (วันที่ 1 หรือ 16 ที่ใกล้ที่สุด
    ที่ >= today) ถ้า today ตรงกับวันออกสลากพอดี จะคืนค่า today เอง (เหลืออีก 0 วัน)"""
    if today.day <= 1:
        return today.replace(day=1)
    elif today.day <= 16:
        return today.replace(day=16)
    else:
        year, month = today.year, today.month
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
        return date(year, month, 1)


# ========== ส่วนราคาทอง / น้ำมัน / ค่าเงิน ==========
# ใช้ API สาธารณะฟรี ไม่ต้องใช้ API key (ทดสอบเชื่อมต่อจริงไม่ได้จากเครื่อง dev
# เพราะโดเมนเหล่านี้ไม่อยู่ใน network allowlist ที่นี่ แต่ครอบ try/except ไว้ให้ครบ)


def get_gold_price():
    """ดึงราคาทองคำล่าสุด (crawl มาจาก goldtraders.or.th ผ่าน api.chnwt.dev ฟรี ไม่ต้องใช้ key)"""
    url = "https://api.chnwt.dev/thai-gold-api/latest"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    resp = data.get("response", {}) or {}
    price = resp.get("price", {}) or {}
    return {
        "update_date": resp.get("update_date", "?"),
        "update_time": resp.get("update_time", "?"),
        "ornament_buy": (price.get("gold") or {}).get("buy", "n/a"),
        "ornament_sell": (price.get("gold") or {}).get("sell", "n/a"),
        "bar_buy": (price.get("gold_bar") or {}).get("buy", "n/a"),
        "bar_sell": (price.get("gold_bar") or {}).get("sell", "n/a"),
    }


def get_oil_price():
    """
    ดึงราคาน้ำมันล่าสุด (crawl มาจาก gasprice.kapook.com ผ่าน api.chnwt.dev ฟรี ไม่ต้องใช้ key)
    เลือกใช้ราคาสถานี ปตท. เป็นตัวแทน (station key 'ptt')
    """
    url = "https://api.chnwt.dev/thai-oil-api/latest"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    resp = data.get("response", {}) or {}
    stations = resp.get("stations", {}) or {}
    ptt = stations.get("ptt", {}) or {}

    def _price(key):
        return (ptt.get(key) or {}).get("price", "n/a")

    return {
        "date": resp.get("date", "?"),
        "gasoline_95": _price("gasoline_95"),
        "gasohol_95": _price("gasohol_95"),
        "gasohol_91": _price("gasohol_91"),
        "diesel": _price("diesel"),
    }


def get_usd_thb_rate():
    """ดึงอัตราแลกเปลี่ยน USD/THB ล่าสุดจาก open.er-api.com (ฟรี ไม่ต้องใช้ API key)"""
    url = "https://open.er-api.com/v6/latest/USD"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("result") != "success":
        return None
    rate = (data.get("rates") or {}).get("THB")
    if rate is None:
        return None
    return {
        "rate": round(float(rate), 2),
        "updated_utc": data.get("time_last_update_utc", "?"),
    }


def build_market_prices_section():
    """
    สรุปราคาทองคำ / น้ำมัน / อัตราแลกเปลี่ยน USD-THB ล่าสุด ณ เวลาที่แจ้งเตือน
    ถ้าดึงข้อมูลแหล่งไหนไม่สำเร็จ จะข้ามเฉพาะส่วนนั้นไปแบบเงียบๆ (log [WARN] ไว้) ไม่ทำให้ข้อความอื่นพัง
    """
    lines = ["", "── ราคาทอง / น้ำมัน / ค่าเงิน 💰 ──"]
    has_any_data = False

    try:
        gold = get_gold_price()
        lines.append(f"🥇 ทองคำแท่ง: รับซื้อ {gold['bar_buy']} / ขายออก {gold['bar_sell']} บาท")
        lines.append(f"📿 ทองรูปพรรณ: รับซื้อ {gold['ornament_buy']} / ขายออก {gold['ornament_sell']} บาท")
        lines.append(f"   (สมาคมค้าทองคำ ณ {gold['update_date']} {gold['update_time']})")
        has_any_data = True
    except Exception as e:
        print(f"[WARN] ดึงราคาทองคำไม่สำเร็จ ({e})")

    try:
        oil = get_oil_price()
        lines.append(
            f"⛽ เบนซิน 95: {oil['gasoline_95']} | แก๊สโซฮอล์ 95: {oil['gasohol_95']} | "
            f"แก๊สโซฮอล์ 91: {oil['gasohol_91']} บาท/ลิตร"
        )
        lines.append(f"🚛 ดีเซล: {oil['diesel']} บาท/ลิตร  (ราคาสถานี ปตท. ณ {oil['date']})")
        has_any_data = True
    except Exception as e:
        print(f"[WARN] ดึงราคาน้ำมันไม่สำเร็จ ({e})")

    try:
        fx = get_usd_thb_rate()
        if fx:
            lines.append(f"💵 ค่าเงินบาท: 1 USD ≈ {fx['rate']} บาท")
            has_any_data = True
    except Exception as e:
        print(f"[WARN] ดึงอัตราแลกเปลี่ยนไม่สำเร็จ ({e})")

    if not has_any_data:
        return None

    return "\n".join(lines)


# ========== ส่วนข้อความให้กำลังใจประจำวัน ==========
# ข้อความทั้งหมดแต่งขึ้นเอง (ไม่ใช่คำคมของบุคคลจริง) เพื่อเลี่ยงปัญหาลิขสิทธิ์/การอ้างอิงผิดคน
# สุ่มใหม่ทุกครั้งที่แจ้งเตือน (ไม่ล็อกตายตัวต่อวันเหมือนเลขเสี่ยงดวง)

ENCOURAGEMENT_QUOTES = [
    # -- ก้าวเล็กๆ / ความก้าวหน้า --
    ("Small steps every day still take you far.", "ก้าวเล็กๆ ในแต่ละวัน ก็พาไปได้ไกลเหมือนกัน"),
    ("Progress is still progress, no matter how small.", "ความก้าวหน้ายังคงเป็นความก้าวหน้า ไม่ว่าจะเล็กแค่ไหน"),
    ("One small step today is enough.", "แค่ก้าวเล็กๆ วันนี้ก็เพียงพอแล้ว"),
    ("You don't need a big leap, just the next small step.", "ไม่ต้องกระโดดไกล แค่ก้าวถัดไปทีละก้าวก็พอ"),
    ("Slow progress beats no progress at all.", "ก้าวหน้าช้าๆ ก็ยังดีกว่าไม่ก้าวหน้าเลย"),
    ("Every little effort adds up over time.", "ความพยายามเล็กๆ น้อยๆ สะสมกันได้เสมอ"),
    ("You're closer today than you were yesterday.", "วันนี้คุณใกล้เป้าหมายกว่าเมื่อวานแล้ว"),
    ("Small wins deserve to be celebrated too.", "ชัยชนะเล็กๆ ก็สมควรได้รับการฉลองเหมือนกัน"),
    ("Keep stacking small good days together.", "สะสมวันดีๆ เล็กๆ ไปเรื่อยๆ นะ"),
    ("A little progress each day adds up to big results.", "ความคืบหน้าเล็กน้อยในแต่ละวัน รวมกันแล้วกลายเป็นผลลัพธ์ที่ยิ่งใหญ่ได้"),
    ("You don't have to finish today, just continue.", "ไม่ต้องทำให้เสร็จวันนี้ก็ได้ แค่ทำต่อไปก็พอ"),
    ("Even a tiny step forward is still forward.", "แม้แต่ก้าวเล็กๆ ไปข้างหน้า ก็ยังถือว่าไปข้างหน้าอยู่ดี"),
    ("Trust the process, one day at a time.", "เชื่อในกระบวนการ ทำไปทีละวันก็พอ"),
    ("You are building something, even on quiet days.", "คุณกำลังสร้างบางอย่างอยู่ แม้ในวันที่เงียบๆ ก็ตาม"),
    ("Consistency matters more than speed.", "ความสม่ำเสมอสำคัญกว่าความเร็ว"),
    ("Today's effort is tomorrow's foundation.", "ความพยายามวันนี้ คือรากฐานของพรุ่งนี้"),
    ("You've come further than you give yourself credit for.", "คุณมาไกลกว่าที่คุณให้เครดิตตัวเองไว้เยอะ"),
    ("Don't compare your chapter one to someone else's chapter ten.", "อย่าเอาบทที่หนึ่งของคุณไปเทียบกับบทที่สิบของคนอื่น"),
    ("Growth doesn't always look like progress, but it counts.", "การเติบโตไม่ได้ดูเหมือนความก้าวหน้าเสมอไป แต่มันก็นับ"),
    ("Just keep going, even slowly.", "แค่ทำต่อไป ต่อให้ช้าก็ไม่เป็นไร"),
    # -- ใจดีกับตัวเอง --
    ("Be as kind to yourself as you are to others.", "ใจดีกับตัวเองบ้าง เหมือนที่ใจดีกับคนอื่น"),
    ("You are allowed to be gentle with yourself today.", "วันนี้อนุญาตให้ตัวเองอ่อนโยนกับตัวเองได้"),
    ("Kindness toward yourself is not a weakness.", "ใจดีกับตัวเองไม่ใช่ความอ่อนแอ"),
    ("You don't have to be perfect to be worthy.", "ไม่ต้องสมบูรณ์แบบก็มีค่าพอในตัวเองอยู่แล้ว"),
    ("Speak to yourself like someone you love.", "พูดกับตัวเองเหมือนพูดกับคนที่คุณรัก"),
    ("It's okay to forgive yourself for today's mistakes.", "ให้อภัยตัวเองสำหรับความผิดพลาดวันนี้ได้นะ"),
    ("You are more than your worst day.", "คุณเป็นมากกว่าวันที่แย่ที่สุดของคุณ"),
    ("Self-care isn't selfish, it's necessary.", "การดูแลตัวเองไม่ใช่ความเห็นแก่ตัว แต่มันจำเป็น"),
    ("You deserve the same patience you give to others.", "คุณสมควรได้รับความอดทนแบบเดียวกับที่ให้คนอื่น"),
    ("You are doing the best you can, and that matters.", "คุณกำลังทำดีที่สุดเท่าที่ทำได้ และนั่นสำคัญ"),
    ("Not liking yourself today doesn't mean you're unworthy.", "ไม่ชอบตัวเองในวันนี้ ไม่ได้แปลว่าคุณไม่มีค่า"),
    ("Give yourself the grace you'd give a friend.", "ให้อภัยตัวเองแบบที่คุณให้เพื่อนบ้าง"),
    ("You are allowed to take up space.", "คุณมีสิทธิ์ที่จะมีตัวตนอยู่ตรงนี้"),
    ("Your worth isn't measured by your productivity.", "คุณค่าของคุณไม่ได้วัดจากผลงานที่ทำได้"),
    ("You are enough, even on an ordinary day.", "คุณดีพอแล้ว แม้ในวันธรรมดาๆ"),
    ("Treat yourself like someone worth taking care of.", "ดูแลตัวเองเหมือนดูแลคนที่คุณค่าควรแก่การดูแล"),
    ("It's okay to need rest without justifying it.", "พักได้โดยไม่ต้องหาเหตุผลมาอธิบาย"),
    ("You are not behind in life, you're on your own timeline.", "คุณไม่ได้ล้าหลังชีวิตใคร คุณแค่มีเส้นทางเวลาของตัวเอง"),
    ("Your feelings are valid, even the uncomfortable ones.", "ความรู้สึกของคุณมีค่าเสมอ แม้จะเป็นความรู้สึกที่ไม่สบายใจก็ตาม"),
    ("You can be a work in progress and still be loved.", "คุณเป็นงานที่ยังไม่เสร็จได้ และยังคงเป็นที่รักได้เหมือนกัน"),
    # -- การพักผ่อน --
    ("Rest is not the opposite of progress, it's part of it.", "การพักผ่อนไม่ใช่ตรงข้ามความก้าวหน้า แต่เป็นส่วนหนึ่งของมัน"),
    ("Take the rest you need without feeling guilty.", "พักได้เท่าที่ร่างกายต้องการ ไม่ต้องรู้สึกผิด"),
    ("A slow morning can still lead to a good day.", "เช้าที่ช้าหน่อย ก็ยังนำไปสู่วันที่ดีได้"),
    ("Not every day needs to be productive to be meaningful.", "ไม่จำเป็นทุกวันต้องมีผลงาน ถึงจะมีความหมาย"),
    ("Rest now, so you can keep going later.", "พักตอนนี้ เพื่อจะได้ไปต่อได้ในภายหลัง"),
    ("It's okay to slow down when you need to.", "ช้าลงได้เมื่อคุณต้องการ ไม่เป็นไรเลย"),
    ("Doing nothing for a while is sometimes exactly what you need.", "บางครั้งการไม่ทำอะไรเลยสักพัก ก็คือสิ่งที่คุณต้องการพอดี"),
    ("Recharge without apology.", "ชาร์จพลังใหม่ได้โดยไม่ต้องขอโทษใคร"),
    ("Even machines need to pause sometimes, so do you.", "แม้แต่เครื่องจักรยังต้องหยุดพักบ้าง คุณก็เช่นกัน"),
    ("A pause is not the same as giving up.", "การหยุดพัก ไม่เหมือนกับการยอมแพ้"),
    ("You've earned a little quiet time today.", "วันนี้คุณสมควรได้เวลาเงียบๆ สักหน่อย"),
    ("Breathing slowly counts as doing something good for yourself.", "แค่หายใจช้าๆ ก็ถือว่าทำสิ่งดีให้ตัวเองแล้ว"),
    ("Let today be lighter than yesterday.", "ขอให้วันนี้เบาสบายกว่าเมื่อวาน"),
    ("Naps are productive too, in their own way.", "การงีบหลับก็มีประโยชน์ในแบบของมันเหมือนกัน"),
    ("You don't owe anyone constant energy.", "คุณไม่จำเป็นต้องมีพลังเต็มร้อยตลอดเวลาให้ใคร"),
    ("Stillness can be its own kind of progress.", "ความนิ่งเงียบก็เป็นความก้าวหน้าในแบบของมันได้"),
    ("Give your mind permission to rest today.", "อนุญาตให้ใจของคุณได้พักบ้างในวันนี้"),
    ("Slow down, you're not behind.", "ช้าลงหน่อยก็ได้ คุณไม่ได้ล้าหลังใคร"),
    ("A calm afternoon is still a good afternoon.", "บ่ายที่เงียบสงบ ก็ยังเป็นบ่ายที่ดีอยู่ดี"),
    ("Taking a break is part of taking care of yourself.", "การพักคือส่วนหนึ่งของการดูแลตัวเอง"),
    # -- ความหวัง / พรุ่งนี้ --
    ("The sun rises again tomorrow, no matter how today goes.", "พรุ่งนี้พระอาทิตย์ก็ขึ้นอีกครั้งเสมอ ไม่ว่าวันนี้จะเป็นยังไง"),
    ("Every storm runs out of rain eventually.", "พายุทุกลูก สักวันก็หมดฝนไปเอง"),
    ("Tomorrow is a fresh page, unwritten.", "พรุ่งนี้คือหน้ากระดาษใหม่ที่ยังไม่ได้เขียนอะไรลงไป"),
    ("Better days are still ahead of you.", "วันที่ดีกว่ายังรอคุณอยู่ข้างหน้า"),
    ("Hope doesn't have to be loud to be real.", "ความหวังไม่ต้องดังก็เป็นของจริงได้"),
    ("This hard season won't last forever.", "ช่วงเวลายากๆ นี้ไม่ได้อยู่กับคุณตลอดไปหรอก"),
    ("Things can still turn around, even now.", "ทุกอย่างยังพลิกกลับมาดีได้ แม้ตอนนี้จะยาก"),
    ("A little light is enough to keep walking.", "แค่แสงสว่างนิดเดียว ก็เพียงพอให้เดินต่อไปได้"),
    ("You don't have to see the whole path, just the next step.", "ไม่ต้องเห็นทางทั้งหมดก็ได้ แค่เห็นก้าวถัดไปก็พอ"),
    ("Even cloudy days end with a sunset somewhere.", "วันที่มีเมฆครึ้ม ก็ยังจบลงด้วยพระอาทิตย์ตกที่ไหนสักแห่งเสมอ"),
    ("Keep a little room for hope today.", "เก็บที่ว่างเล็กๆ ไว้สำหรับความหวังในวันนี้ด้วยนะ"),
    ("What feels impossible today may feel lighter tomorrow.", "สิ่งที่รู้สึกเป็นไปไม่ได้วันนี้ พรุ่งนี้อาจรู้สึกเบาลงก็ได้"),
    ("New mornings bring new chances.", "เช้าวันใหม่มาพร้อมโอกาสใหม่เสมอ"),
    ("This chapter isn't your whole story.", "บทนี้ไม่ใช่เรื่องราวทั้งหมดของคุณ"),
    ("Somewhere ahead, there's a good day waiting for you.", "ข้างหน้ามีวันดีๆ รอคุณอยู่แน่นอน"),
    ("You're allowed to hope for something better.", "คุณมีสิทธิ์หวังถึงสิ่งที่ดีกว่าได้เสมอ"),
    ("Even in the dark, morning is still coming.", "แม้ในความมืด เช้าก็ยังคงมาถึงเสมอ"),
    ("Hold on a little longer, change is often close.", "อดทนอีกนิด บางทีความเปลี่ยนแปลงก็อยู่ใกล้แค่เอื้อม"),
    ("The bad days pass, just like the good ones do.", "วันแย่ๆ ก็ผ่านไปได้ เหมือนวันดีๆ ที่เคยผ่านมา"),
    ("Keep believing that things can get better.", "เชื่อต่อไปว่าทุกอย่างจะดีขึ้นได้"),
    # -- ความกล้าหาญ --
    ("You are braver than you think, on your hardest days.", "คุณกล้าหาญกว่าที่คิดไว้ แม้ในวันที่ยากที่สุด"),
    ("Facing a hard day already takes courage.", "แค่กล้าเผชิญวันที่ยากลำบาก ก็ต้องใช้ความกล้าหาญแล้ว"),
    ("It's okay to feel scared and keep going anyway.", "กลัวได้ แต่ก็ยังไปต่อได้เหมือนกัน"),
    ("Every brave thing starts as a small, shaky step.", "เรื่องกล้าหาญทุกเรื่องเริ่มจากก้าวเล็กๆ ที่สั่นเทาเสมอ"),
    ("You made it through every hard day so far. That's proof enough.", "ผ่านวันยากๆ มาได้ทุกครั้ง นั่นแหละคือหลักฐานว่าคุณไหว"),
    ("Asking for help takes real courage.", "การขอความช่วยเหลือ ก็ต้องใช้ความกล้าหาญจริงๆ"),
    ("You don't have to feel ready to start.", "ไม่ต้องรู้สึกพร้อมก็เริ่มได้"),
    ("Standing up again after falling counts as strength.", "ลุกขึ้นใหม่หลังจากล้ม ก็นับเป็นความแข็งแกร่งแล้ว"),
    ("Uncertainty is uncomfortable, but you can sit with it.", "ความไม่แน่นอนมันอึดอัด แต่คุณอยู่กับมันได้"),
    ("You've handled hard things before, you can handle this too.", "คุณเคยผ่านเรื่องยากมาก่อน ครั้งนี้ก็ผ่านได้เหมือนกัน"),
    ("Courage doesn't mean feeling no fear at all.", "ความกล้าหาญไม่ได้แปลว่าไม่มีความกลัวเลย"),
    ("Trying again after a setback is its own kind of brave.", "ลองใหม่หลังจากล้มเหลว ก็คือความกล้าในแบบหนึ่ง"),
    ("Small acts of courage still count.", "การกล้าหาญเล็กๆ น้อยๆ ก็ยังนับเสมอ"),
    ("You are allowed to take things one brave step at a time.", "คุณกล้าได้ทีละก้าวก็พอ ไม่ต้องรีบ"),
    ("Facing today is enough, you don't have to solve everything at once.", "แค่เผชิญกับวันนี้ก็พอแล้ว ไม่ต้องแก้ทุกอย่างพร้อมกัน"),
    ("You're stronger than the thing that's worrying you.", "คุณแข็งแกร่งกว่าเรื่องที่กำลังกังวลอยู่"),
    ("It takes courage to keep trying after disappointment.", "ต้องใช้ความกล้าเพื่อจะลองอีกครั้งหลังจากผิดหวัง"),
    ("Being honest about struggling is a brave thing to do.", "การยอมรับตรงๆ ว่ากำลังลำบากอยู่ ก็เป็นเรื่องที่กล้าหาญ"),
    ("You don't need to be fearless, just willing to try.", "ไม่ต้องไม่กลัวเลยก็ได้ แค่เต็มใจจะลองก็พอ"),
    ("Every time you keep going, you prove you're capable.", "ทุกครั้งที่คุณไปต่อ คุณก็พิสูจน์ว่าคุณทำได้"),
    # -- ความสุขเล็กๆ / ขอบคุณ --
    ("One good moment is enough to make a day worth it.", "แค่ช่วงเวลาดีๆ สักครั้งเดียว ก็ทำให้วันนั้นคุ้มค่าแล้ว"),
    ("A little sunshine can change the whole mood of a day.", "แดดนิดเดียวก็เปลี่ยนอารมณ์ทั้งวันได้"),
    ("Notice the small good things, they add up.", "สังเกตสิ่งดีๆ เล็กๆ น้อยๆ ไว้ มันสะสมกันได้"),
    ("A warm cup of something can be a small comfort today.", "เครื่องดื่มอุ่นๆ สักแก้ว ก็เป็นความอบอุ่นเล็กๆ ของวันนี้ได้"),
    ("Simple moments often hold the most warmth.", "ช่วงเวลาธรรมดาๆ มักเก็บความอบอุ่นไว้มากที่สุด"),
    ("Being grateful for little things makes room for more of them.", "รู้สึกขอบคุณกับเรื่องเล็กๆ ก็เปิดทางให้เรื่องดีๆ เข้ามาอีก"),
    ("A kind word today can be someone's whole day.", "คำพูดดีๆ วันนี้ อาจเป็นทั้งวันที่ดีของใครสักคน"),
    ("There's beauty in ordinary days too.", "ความสวยงามมีอยู่ในวันธรรมดาด้วยเหมือนกัน"),
    ("Let yourself enjoy the small things without guilt.", "ปล่อยให้ตัวเองมีความสุขกับเรื่องเล็กๆ โดยไม่ต้องรู้สึกผิด"),
    ("A good meal, a kind message, a quiet moment, all count.", "มื้ออาหารดีๆ ข้อความอบอุ่น ช่วงเวลาเงียบสงบ ล้วนมีค่าทั้งนั้น"),
    ("Slow down enough to notice something good today.", "ช้าลงสักนิด เพื่อสังเกตสิ่งดีๆ ที่เกิดขึ้นวันนี้"),
    ("Even small comforts deserve to be appreciated.", "ความสบายใจเล็กๆ ก็สมควรได้รับการขอบคุณ"),
    ("The little joys are still joys.", "ความสุขเล็กๆ ก็ยังเป็นความสุขอยู่ดี"),
    ("A good laugh can lighten even a heavy day.", "เสียงหัวเราะดีๆ ก็ทำให้วันที่หนักอึ้งเบาลงได้"),
    ("Thank yourself for making it through today.", "ขอบคุณตัวเองที่ผ่านวันนี้มาได้"),
    ("There's always something small worth being thankful for.", "มักจะมีเรื่องเล็กๆ ที่ควรค่าแก่การขอบคุณเสมอ"),
    ("Appreciate the quiet wins nobody else sees.", "ชื่นชมชัยชนะเงียบๆ ที่ไม่มีใครเห็นบ้าง"),
    ("A gentle breeze, a good song, a nice memory, small gifts of the day.", "สายลมเย็นๆ เพลงเพราะๆ ความทรงจำดีๆ คือของขวัญเล็กๆ ของวันนี้"),
    ("Today had at least one good part, hold onto that.", "วันนี้มีอย่างน้อยหนึ่งช่วงที่ดี เก็บมันไว้นะ"),
    ("Gratitude turns an ordinary day into something warmer.", "ความรู้สึกขอบคุณ เปลี่ยนวันธรรมดาให้อบอุ่นขึ้นได้"),
    # -- ความอดทน / การเติบโต --
    ("Growth is quiet most of the time, keep going.", "การเติบโตส่วนใหญ่มันเงียบๆ แบบนี้แหละ สู้ต่อไปนะ"),
    ("Even the smallest plant needs time to grow roots.", "แม้แต่ต้นเล็กๆ ก็ยังต้องใช้เวลาหยั่งราก"),
    ("Good things take time, and that's okay.", "สิ่งดีๆ ต้องใช้เวลา และนั่นก็ไม่เป็นไร"),
    ("You're allowed to grow at your own pace.", "คุณเติบโตในจังหวะของตัวเองได้"),
    ("Not every season is for blooming, some are for rooting.", "ไม่ใช่ทุกฤดูกาลที่ต้องออกดอก บางฤดูก็มีไว้สำหรับหยั่งราก"),
    ("Patience with yourself is a quiet kind of strength.", "ความอดทนกับตัวเอง ก็เป็นความแข็งแกร่งแบบเงียบๆ"),
    ("Some things grow best when left alone for a while.", "บางอย่างก็เติบโตได้ดีที่สุด เมื่อถูกปล่อยไว้สักพัก"),
    ("Change rarely happens overnight, and that's normal.", "การเปลี่ยนแปลงมักไม่เกิดในชั่วข้ามคืน และนั่นก็ปกติดี"),
    ("You are allowed to still be figuring things out.", "คุณยังค้นหาคำตอบอยู่ได้ ไม่เป็นไรเลย"),
    ("Growth doesn't always feel like progress while it's happening.", "การเติบโตไม่ได้รู้สึกเหมือนความก้าวหน้าเสมอไป ตอนที่มันกำลังเกิดขึ้น"),
    ("Trust the timing of your own life.", "เชื่อในจังหวะเวลาของชีวิตตัวเองบ้าง"),
    ("Every expert was once a beginner too.", "ผู้เชี่ยวชาญทุกคนก็เคยเป็นมือใหม่มาก่อนทั้งนั้น"),
    ("It's okay if today is just for practicing, not perfecting.", "วันนี้แค่ฝึกฝนก็พอ ไม่ต้องสมบูรณ์แบบ"),
    ("Roots grow in the dark before anything blooms above ground.", "รากเติบโตอยู่ในความมืดก่อนที่อะไรจะผลิบานเหนือดิน"),
    ("You're not late, you're right on time for your own path.", "คุณไม่ได้มาสาย คุณมาถูกเวลาสำหรับเส้นทางของตัวเอง"),
    ("Some lessons take longer to learn, and that's fine.", "บางบทเรียนก็ใช้เวลาเรียนรู้นานกว่า และนั่นก็โอเค"),
    ("Keep tending to your own growth quietly.", "ดูแลการเติบโตของตัวเองไปเงียบๆ ต่อไปนะ"),
    ("A little patience today saves a lot of frustration tomorrow.", "อดทนอีกนิดวันนี้ ช่วยลดความหงุดหงิดในวันข้างหน้าได้เยอะ"),
    ("You're allowed to take the long way if it's the right way.", "เลือกเดินทางไกลได้ ถ้ามันเป็นทางที่ถูกต้องสำหรับคุณ"),
    ("Give yourself permission to still be learning.", "อนุญาตให้ตัวเองยังคงเรียนรู้อยู่ได้เสมอ"),
    # -- การปล่อยวาง --
    ("It's okay to let go of what no longer serves you.", "ปล่อยวางสิ่งที่ไม่มีประโยชน์กับคุณอีกต่อไปได้นะ"),
    ("Holding on tightly isn't always strength.", "การยึดติดแน่นๆ ไม่ได้แปลว่าแข็งแกร่งเสมอไป"),
    ("You can forgive without forgetting the lesson.", "ให้อภัยได้ โดยไม่ต้องลืมบทเรียนที่ได้รับ"),
    ("Releasing what hurts you is an act of self-care.", "ปล่อยสิ่งที่ทำให้เจ็บปวดไป ก็คือการดูแลตัวเองแบบหนึ่ง"),
    ("You don't have to carry yesterday's weight into today.", "ไม่ต้องแบกน้ำหนักของเมื่อวาน มาไว้ในวันนี้ก็ได้"),
    ("Letting go doesn't mean the memory didn't matter.", "การปล่อยวาง ไม่ได้แปลว่าความทรงจำนั้นไม่มีความหมาย"),
    ("It's okay to close a chapter that's no longer good for you.", "ปิดบทที่ไม่ดีต่อคุณอีกต่อไปได้เลย"),
    ("Forgiving yourself is part of moving forward.", "ให้อภัยตัวเอง คือส่วนหนึ่งของการก้าวไปข้างหน้า"),
    ("You can release the outcome and still have done your best.", "ปล่อยผลลัพธ์ไปได้ ถึงแม้คุณจะทำเต็มที่แล้วก็ตาม"),
    ("Some things are meant to be set down, not fixed.", "บางอย่างมีไว้ให้วางลง ไม่ใช่ให้ซ่อมแซม"),
    ("You are not required to hold onto every hurt.", "คุณไม่จำเป็นต้องแบกความเจ็บปวดทุกอย่างไว้"),
    ("Letting go can feel like loss and relief at the same time.", "การปล่อยวาง อาจรู้สึกเหมือนสูญเสียและโล่งใจไปพร้อมกันได้"),
    ("You are allowed to walk away from what drains you.", "เดินออกจากสิ่งที่ทำให้คุณหมดพลังได้เลย"),
    ("Peace is sometimes found in letting go, not winning.", "บางครั้งความสงบก็เจอได้จากการปล่อยวาง ไม่ใช่จากการชนะ"),
    ("You can love someone or something and still let it go.", "คุณรักใครสักคนหรือบางอย่างได้ และยังปล่อยมันไปได้เหมือนกัน"),
    ("Forgiveness is a gift you give yourself too.", "การให้อภัย ก็เป็นของขวัญที่คุณให้กับตัวเองด้วยเหมือนกัน"),
    ("It's okay to stop carrying what isn't yours to carry.", "หยุดแบกสิ่งที่ไม่ใช่หน้าที่ของคุณได้เลย"),
    ("You can release the need to have all the answers today.", "ปล่อยความต้องการที่จะมีคำตอบทุกอย่างวันนี้ไปได้"),
    ("Not every battle needs to be fought to feel at peace.", "ไม่ใช่ทุกสมรภูมิที่ต้องสู้ ถึงจะรู้สึกสงบได้"),
    ("Sometimes the bravest thing is simply letting it go.", "บางครั้งเรื่องที่กล้าหาญที่สุด ก็แค่การปล่อยมันไป"),
    # -- การเริ่มต้นใหม่ --
    ("You are allowed to start again, as many times as you need.", "เริ่มใหม่ได้เสมอ กี่ครั้งก็ได้เท่าที่ต้องการ"),
    ("Every ending makes room for a new beginning.", "ทุกการจบ เปิดทางให้กับการเริ่มต้นใหม่เสมอ"),
    ("It's never too late to try something new.", "ไม่มีคำว่าสายเกินไปสำหรับการลองสิ่งใหม่ๆ"),
    ("A fresh start doesn't need a perfect plan.", "การเริ่มต้นใหม่ ไม่จำเป็นต้องมีแผนที่สมบูรณ์แบบ"),
    ("You can rewrite today, even if yesterday went badly.", "เขียนวันนี้ใหม่ได้ ถึงแม้เมื่อวานจะแย่ก็ตาม"),
    ("Beginnings are allowed to be messy and uncertain.", "การเริ่มต้น อนุญาตให้ยุ่งเหยิงและไม่แน่นอนได้"),
    ("You get to choose who you become from here.", "คุณเลือกได้ว่าจะเป็นใครต่อจากนี้"),
    ("Today can be day one of something good.", "วันนี้อาจเป็นวันแรกของบางอย่างที่ดีก็ได้"),
    ("New beginnings often start quietly, without fanfare.", "การเริ่มต้นใหม่มักเริ่มอย่างเงียบๆ โดยไม่มีพิธีรีตอง"),
    ("You are not the same person you were yesterday, and that's growth.", "คุณไม่ใช่คนเดิมเมื่อวานแล้ว และนั่นคือการเติบโต"),
    ("Starting over isn't failure, it's often wisdom.", "การเริ่มใหม่ไม่ใช่ความล้มเหลว บ่อยครั้งมันคือความฉลาด"),
    ("Give this new chapter a fair chance.", "ให้โอกาสบทใหม่นี้อย่างเต็มที่"),
    ("Every sunrise is an invitation to begin again.", "พระอาทิตย์ขึ้นทุกครั้ง คือคำเชิญให้เริ่มต้นใหม่อีกครั้ง"),
    ("You can leave the old story behind whenever you're ready.", "ทิ้งเรื่องราวเก่าไว้ข้างหลังได้ เมื่อคุณพร้อม"),
    ("First steps are allowed to feel wobbly.", "ก้าวแรกๆ อนุญาตให้รู้สึกไม่มั่นคงได้"),
    ("A new beginning doesn't erase the old one, it builds on it.", "การเริ่มต้นใหม่ ไม่ได้ลบล้างของเก่า แต่ต่อยอดจากมัน"),
    ("You're allowed to want something different now.", "คุณอยากได้อะไรที่ต่างไปตอนนี้ก็ได้ ไม่ผิดอะไร"),
    ("Change can be scary and still be exactly what you need.", "การเปลี่ยนแปลงอาจน่ากลัว แต่ก็ยังเป็นสิ่งที่คุณต้องการพอดี"),
    ("Today is a good day to plant a new seed.", "วันนี้เป็นวันที่ดีสำหรับการปลูกเมล็ดพันธุ์ใหม่"),
    ("Whatever happened before, you can still choose your next chapter.", "ไม่ว่าอะไรจะเกิดขึ้นมาก่อน คุณยังเลือกบทต่อไปของตัวเองได้เสมอ"),
]


def build_encouragement_section():
    """
    เลือกข้อความให้กำลังใจ (อังกฤษ + คำแปลไทย) แบบสุ่มจริง เปลี่ยนทุกครั้งที่แจ้งเตือน
    ข้อความทั้งหมดแต่งขึ้นเอง ไม่ใช่คำคมของบุคคลจริง
    """
    quote_en, quote_th = random.choice(ENCOURAGEMENT_QUOTES)
    lines = [
        "",
        "คุณเก่ง ให้แจ้ง ข้อความให้กำลังใจ 💛 ในทุกๆวันให้กับทุกคนครับ :)",
        f'"{quote_en}"',
        f"({quote_th})",
    ]
    return "\n".join(lines)


def build_lucky_numbers_section():
    """
    คำนวณเลขท้าย 2 ตัว / 3 ตัว 'ครั้งเดียวต่อวัน' โดยอิง seed ที่รวมหลายปัจจัย
    ซึ่งคงที่ตลอดทั้งวันนั้น (วันที่ปัจจุบัน + พิกัดตำแหน่ง + ชื่อพื้นที่) แล้วแฮชรวมกัน
    เป็นเลข seed คงที่ ผลลัพธ์จะ 'เหมือนเดิมทุกครั้ง' ที่รันซ้ำในวันเดียวกัน
    (ไม่ว่าจะรันกี่รอบก็ตาม) แต่จะเปลี่ยนไปเองโดยอัตโนมัติเมื่อข้ามวัน
    ไม่มีผิดไม่มีถูก ไม่ได้อิงสถิติหรือพยากรณ์ผลจริงแต่อย่างใด เป็นความบันเทิงเท่านั้น
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    days_left = (next_draw_date(datetime.now().date()) - datetime.now().date()).days
    # รวมหลายปัจจัยเข้าด้วยกันเป็น seed: วันที่ + พิกัด + ชื่อพื้นที่
    seed_source = f"{today_str}|{LATITUDE}|{LONGITUDE}|{LOCATION_NAME}"
    seed_hash = hashlib.sha256(seed_source.encode("utf-8")).hexdigest()
    seed_int = int(seed_hash, 16)

    # ใช้ random.Random instance แยกต่างหาก (ไม่แตะ global random state)
    # เพื่อให้ผลลัพธ์เชิงกำหนด (deterministic) ตาม seed ที่คำนวณไว้
    rng = random.Random(seed_int)
    two_digit = f"{rng.randint(0, 99):02d}"
    three_digit = f"{rng.randint(0, 999):03d}"

    lines = [
        "",
        f"── เลขสุ่มเสี่ยงดวงประจำวัน 🎲 (อีก {days_left} วัน สลากออก) ──",
        f"🔢 เลขท้าย 2 ตัว: {two_digit}",
        f"🔢 เลขท้าย 3 ตัว: {three_digit}",
        "⚠️ คำนวณครั้งเดียวต่อวัน ไม่เปลี่ยนถ้ารันซ้ำวันเดียวกัน ไม่มีผิดไม่มีถูก ไม่ได้อิงสถิติหรือพยากรณ์ผลจริงนะครับ",
    ]
    return "\n".join(lines)


# ========== รวมข้อความ ==========


def build_message():
    weather = get_weather()
    today = weather["daily"]
    hourly = weather["hourly"]
    current = weather["current"]

    today_str = today["time"][0]

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
        lines.append(f"📍 สถานีที่ใกล้ที่สุด (ที่มีค่า PM2.5): {pm['station_name']} (~{pm['distance_km']} กม.)")
    else:
        lines.append("")
        lines.append("── คุณภาพอากาศ (PM2.5) ──")
        lines.append("⚠️ ไม่พบสถานีที่มีค่า PM2.5 ในบริเวณใกล้เคียงตอนนี้")

    lines.append("")
    lines.append("── พรุ่งนี้ ──")
    lines.append(f"🌡️ สูงสุด/ต่ำสุด: {today['temperature_2m_max'][1]}°C / {today['temperature_2m_min'][1]}°C")
    lines.append(f"🌧️ โอกาสฝนตก: {today['precipitation_probability_max'][1]}%  (ปริมาณ ~{today['precipitation_sum'][1]} มม.)")

    if ENABLE_LOTTERY:
        lottery_block = build_lottery_section()
        if lottery_block:
            lines.append(lottery_block)

    if ENABLE_LUCKY_NUMBER:
        today = datetime.now().date()
        days_left = (next_draw_date(today) - today).days
        if 1 <= days_left <= 7:
            lines.append(build_lucky_numbers_section())

    if ENABLE_MARKET_PRICES:
        market_block = build_market_prices_section()
        if market_block:
            lines.append(market_block)

    lines.append(build_encouragement_section())

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
