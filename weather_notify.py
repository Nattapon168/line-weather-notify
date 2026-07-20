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
import time
import requests
import urllib3
from collections import Counter
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

# เปิด/ปิดส่วนสลากกินแบ่งรัฐบาล (ตั้งเป็น "0" ถ้าไม่ต้องการให้ดึงส่วนนี้)
ENABLE_LOTTERY = os.environ.get("ENABLE_LOTTERY", "1") != "0"

# เปิด/ปิดส่วนเลขสุ่มเสี่ยงดวง (เอาฮาอย่างเดียว ไม่เกี่ยวกับผลจริง)
ENABLE_LUCKY_NUMBER = os.environ.get("ENABLE_LUCKY_NUMBER", "1") != "0"

# เปิด/ปิดส่วนสถิติ "เลขออกบ่อย" ย้อนหลัง ~1 ปี (แสดงคู่กับพยากรณ์อากาศทุกวัน)
ENABLE_HOT_NUMBERS = os.environ.get("ENABLE_HOT_NUMBERS", "1") != "0"
# จำนวนงวดย้อนหลังที่จะดึงมาคำนวณสถิติ (ค่า default 24 งวด ~ 1 ปี เพราะออก 2 งวด/เดือน)
HOT_NUMBERS_LOOKBACK_PERIODS = int(os.environ.get("HOT_NUMBERS_LOOKBACK_PERIODS", 24))
# ไฟล์ cache ผลสถิติ กันไม่ต้องยิง API ซ้ำถ้ารันสคริปต์หลายรอบในวันเดียวกัน
# (มีผลเฉพาะตอนรันบนเครื่อง/ session เดียวกัน เพราะ GitHub Actions checkout repo ใหม่ทุกครั้ง
#  ถ้าอยากให้ cache ข้ามรอบ Actions จริงๆ ต้องเพิ่ม actions/cache ใน workflow แยกต่างหาก)
HOT_NUMBERS_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".lottery_history_cache.json"
)
HOT_NUMBERS_CACHE_TTL_HOURS = 20

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


# ========== ส่วนสถิติ "เลขออกบ่อย" ย้อนหลัง ~1 ปี ==========


def lottery_period_dates_back(n_periods, from_date=None):
    """
    คืน list ของวันที่งวดสลาก (datetime.date) ย้อนหลัง n_periods งวด
    เริ่มนับจากงวดล่าสุดก่อนหน้า from_date (ไม่รวมงวดของวันนี้เอง ถ้ายังไม่ประกาศผล
    ก็แค่ดึงไม่ได้ข้อมูล แล้วโค้ดฝั่งดึงจะข้ามให้เอง ไม่ทำให้พัง)
    """
    if from_date is None:
        from_date = datetime.now().date()

    if from_date.day >= 16:
        cursor = from_date.replace(day=16)
    else:
        cursor = from_date.replace(day=1)

    dates = []
    for _ in range(n_periods):
        dates.append(cursor)
        cursor = previous_period_date(cursor)
    return dates


def fetch_lottery_history(dates, delay_sec=0.3):
    """ดึงผลสลากย้อนหลังตามรายการวันที่ที่ให้มา ข้ามงวดที่ดึงไม่สำเร็จแบบเงียบๆ"""
    results = []
    for d in dates:
        try:
            r = get_lottery_by_date(d)
        except Exception as e:
            print(f"[WARN] ดึงประวัติสลากงวด {d} ไม่สำเร็จ ({e})")
            r = None
        if r:
            results.append(r)
        time.sleep(delay_sec)
    return results


def compute_hot_digits(history, top_n=5):
    """นับความถี่ของแต่ละหลัก (0-9) ที่ปรากฏในเลขท้าย 2 ตัว และเลขท้าย 3 ตัว(ล่าง) ทั้งหมด"""
    counter = Counter()
    for r in history:
        for num in r.get("last2", []):
            counter.update(num)
        for num in r.get("last3b", []):
            counter.update(num)
    return counter.most_common(top_n)


def _load_hot_digits_cache():
    try:
        with open(HOT_NUMBERS_CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
        cached_at = datetime.fromisoformat(cache["cached_at"])
        if datetime.now() - cached_at < timedelta(hours=HOT_NUMBERS_CACHE_TTL_HOURS):
            return [tuple(x) for x in cache["hot_digits"]]
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        pass
    return None


def _save_hot_digits_cache(hot_digits):
    try:
        with open(HOT_NUMBERS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"cached_at": datetime.now().isoformat(), "hot_digits": hot_digits},
                f,
                ensure_ascii=False,
            )
    except OSError as e:
        print(f"[WARN] บันทึก cache สถิติสลากไม่สำเร็จ ({e})")


def build_hot_numbers_section():
    """
    สรุป 'เลขที่ออกบ่อย' โดยนับความถี่ของแต่ละหลัก (0-9) ที่ปรากฏใน
    เลขท้าย 2 ตัว และเลขท้าย 3 ตัว(ล่าง) ย้อนหลังประมาณ 1 ปี (ค่า default 24 งวด)
    เป็นแค่สถิติสนุกๆ ไม่ใช่การพยากรณ์ผลจริง แสดงคู่กับพยากรณ์อากาศทุกวัน
    """
    hot_digits = _load_hot_digits_cache()

    if hot_digits is None:
        dates = lottery_period_dates_back(HOT_NUMBERS_LOOKBACK_PERIODS)
        history = fetch_lottery_history(dates)
        if not history:
            print("[WARN] ดึงประวัติสลากไม่สำเร็จเลยสักงวด ข้ามส่วนสถิติเลขออกบ่อย")
            return None
        hot_digits = compute_hot_digits(history, top_n=5)
        _save_hot_digits_cache(hot_digits)

    if not hot_digits:
        return None

    digits_text = ", ".join(f"{digit} ({count} ครั้ง)" for digit, count in hot_digits)
    lines = [
        "",
        "── สถิติเลขออกบ่อย ย้อนหลัง ~1 ปี 📊 ──",
        f"🔥 หลักเลขที่ออกบ่อยสุด 5 อันดับ: {digits_text}",
        "(นับจากตัวเลขในเลขท้าย 2 ตัว/3 ตัว ย้อนหลัง เป็นสถิติสนุกๆ ไม่ใช่การพยากรณ์)",
    ]
    return "\n".join(lines)


# ========== ส่วนเลขสุ่มเสี่ยงดวง (เอาฮาอย่างเดียว) ==========


def build_lucky_numbers_section():
    """
    สุ่มเลขท้าย 2 ตัว และเลขท้าย 3 ตัว 'เพื่อความบันเทิงเท่านั้น'
    ใช้ random.randint ธรรมดา ไม่มีการอิงสถิติ ไม่มีการพยากรณ์ผลจริง
    ไม่มีผิดไม่มีถูก ไม่เกี่ยวข้องกับผลสลากที่ออกจริงแต่อย่างใด
    """
    two_digit = f"{random.randint(0, 99):02d}"
    three_digit = f"{random.randint(0, 999):03d}"
    lines = [
        "",
        "── เลขสุ่มเสี่ยงดวงประจำวัน 🎲 (เอาฮาอย่างเดียว) ──",
        f"🔢 เลขท้าย 2 ตัว: {two_digit}",
        f"🔢 เลขท้าย 3 ตัว: {three_digit}",
        "⚠️ สุ่มขึ้นมาเล่นๆ ไม่มีผิดไม่มีถูก ไม่ได้อิงสถิติหรือพยากรณ์ผลจริงนะครับ",
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

    if ENABLE_HOT_NUMBERS:
        hot_numbers_block = build_hot_numbers_section()
        if hot_numbers_block:
            lines.append(hot_numbers_block)

    if ENABLE_LUCKY_NUMBER:
        lines.append(build_lucky_numbers_section())

    lines.append("")
    lines.append("ข้อมูล: Open-Meteo / Air4Thai (กรมควบคุมมลพิษ) / GLO (สำนักงานสลากกินแบ่งรัฐบาล)")

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
