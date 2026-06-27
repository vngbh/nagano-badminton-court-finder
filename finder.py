import streamlit as st
import requests
import re
import json
import math
import time
import os
from datetime import date, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_URL = "https://city.nagano.nagano.machikagi-remote.jp"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# 信州大学工学部（長野市若里4丁目17-1）
REF_LAT = 36.6443
REF_LON = 138.1905

COORDS_FILE = os.path.join(os.path.dirname(__file__), "coords_cache.json")


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return round(2 * R * math.asin(math.sqrt(a)), 1)


def load_coords_cache() -> dict:
    if os.path.exists(COORDS_FILE):
        with open(COORDS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_coords_cache(cache: dict) -> None:
    with open(COORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def geocode(address: str, cache: dict) -> tuple[float, float] | None:
    if address in cache:
        return cache[address]
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": f"長野県{address}",
                "format": "json",
                "countrycodes": "jp",
                "limit": 1,
            },
            headers={"User-Agent": "nagano-court-finder/1.0 (local personal tool)"},
            timeout=10,
        )
        results = resp.json()
        if results:
            coords = (float(results[0]["lat"]), float(results[0]["lon"]))
            cache[address] = coords
            return coords
    except Exception:
        pass
    cache[address] = None
    return None


def extract_price(title: str) -> int | None:
    m = re.search(r"[¥￥](\d+)", title)
    if m:
        return int(m.group(1))
    if "無料" in title:
        return 0
    return None


@st.cache_data(ttl=86400, show_spinner="施設・部屋情報を取得中...")
def discover_rooms() -> list[dict]:
    """Fetch all gymnasium facilities and rooms. Cached 24 h."""
    rooms = []
    coords_cache = load_coords_cache()

    for page in range(1, 10):
        resp = requests.get(
            f"{BASE_URL}/facilities",
            params={"tag": "1", "page": page},
            headers=HEADERS,
            timeout=15,
        )
        # Split HTML into per-facility blocks
        blocks = re.split(r'(?=<li class="facility)', resp.text)
        facility_blocks = [b for b in blocks if 'class="facility' in b]
        if not facility_blocks:
            break

        for block in facility_blocks:
            fid_m = re.search(r'href="/facilities/(\d+)"', block)
            fname_m = re.search(r'<a class="trans" href="/facilities/\d+">(.*?)</a>', block)
            addr_m = re.search(r'class="[^"]*room-address[^"]*"><a[^>]*>([^<]+)</a>', block)

            if not fid_m or not fname_m:
                continue

            fid = fid_m.group(1)
            fname = re.sub(r"\s+", " ", fname_m.group(1)).strip()
            address = addr_m.group(1).strip() if addr_m else None

            # Geocode
            coords = None
            if address:
                if address not in coords_cache:
                    time.sleep(1)  # Nominatim: 1 req/sec
                coords = geocode(address, coords_cache)

            distance_km = (
                haversine(REF_LAT, REF_LON, coords[0], coords[1])
                if coords
                else None
            )

            # Get rooms for this facility
            resp2 = requests.get(
                f"{BASE_URL}/rooms",
                params={"facility_id": fid},
                headers=HEADERS,
                timeout=15,
            )
            room_matches = re.findall(
                r'<a class="trans" href="/rooms/(\d+)">(.*?)</a>', resp2.text
            )
            for rid, rname in room_matches:
                rooms.append(
                    {
                        "fid": fid,
                        "fname": fname,
                        "rid": rid,
                        "rname": rname.strip(),
                        "sid": fid,  # requested_setting_id == facility_id
                        "address": address,
                        "distance_km": distance_km,
                    }
                )

    save_coords_cache(coords_cache)
    return rooms


def fetch_slots(room: dict, date_str: str) -> list[dict]:
    start = f"{date_str}T00:00:00+09:00"
    next_day = (
        datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)
    ).strftime("%Y-%m-%d")
    end = f"{next_day}T00:00:00+09:00"
    try:
        resp = requests.get(
            f"{BASE_URL}/rooms/{room['rid']}/reservation_events.json",
            params={"start": start, "end": end, "requested_setting_id": room["sid"]},
            headers=HEADERS,
            timeout=10,
        )
        events = resp.json()
        if not isinstance(events, list):
            return []
        return [
            e
            for e in events
            if e.get("eventStatus") == "reservation" and date_str in e.get("start", "")
        ]
    except Exception:
        return []


def sort_key(r: dict):
    dist = r["距離(km)"] if r["距離(km)"] is not None else 9999
    return (dist, r["施設名"], r["部屋"], r["開始"])


# ── UI ──────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="長野 バドミントンコート空き検索")
st.title("長野市 バドミントンコート 空き検索")
st.caption("基準: 信州大学工学部 / データ取得元: 長野市 施設案内予約システム（ログイン不要）")

col1, col2 = st.columns([3, 1])
with col1:
    selected_date = st.date_input(
        "日付を選択",
        min_value=date.today(),
        value=date.today() + timedelta(days=1),
    )
with col2:
    st.write("")
    st.write("")
    if st.button("施設一覧を更新"):
        discover_rooms.clear()
        st.rerun()

if st.button("空きを検索", type="primary", use_container_width=True):
    date_str = selected_date.strftime("%Y-%m-%d")
    rooms = discover_rooms()

    if not rooms:
        st.error("施設情報を取得できませんでした。しばらくしてから再試行してください。")
        st.stop()

    results: list[dict] = []
    bar = st.progress(0)
    status = st.empty()

    with ThreadPoolExecutor(max_workers=15) as executor:
        futures = {executor.submit(fetch_slots, r, date_str): r for r in rooms}
        for i, future in enumerate(as_completed(futures)):
            room = futures[future]
            for slot in future.result():
                price = extract_price(slot.get("title", ""))
                results.append(
                    {
                        "施設名": room["fname"],
                        "部屋": room["rname"],
                        "開始": slot["start"][11:16],
                        "終了": slot["end"][11:16],
                        "料金": slot.get("title", "").split("\n")[0],
                        "価格": price,
                        "距離(km)": room["distance_km"],
                        "予約": f"{BASE_URL}/rooms/{room['rid']}/reservation_calendar?date={date_str}",
                    }
                )
            bar.progress((i + 1) / len(rooms))
            status.text(f"確認中 {i + 1}/{len(rooms)} 室")

    bar.empty()
    status.empty()

    if not results:
        st.warning(f"{date_str} は空き枠が見つかりませんでした")
        st.stop()

    results.sort(key=sort_key)
    paid = [r for r in results if r["価格"] is None or r["価格"] > 0]
    free = [r for r in results if r["価格"] == 0]

    weekday_jp = ["月", "火", "水", "木", "金", "土", "日"]
    wd = weekday_jp[selected_date.weekday()]
    st.success(f"{date_str}（{wd}）: 有料 {len(paid)} 件 / 無料 {len(free)} 件")

    tab_paid, tab_free = st.tabs([f"有料  ({len(paid)}件)", f"無料  ({len(free)}件)"])

    def render_results(rows: list[dict], container):
        if not rows:
            container.info("該当なし")
            return
        for r in rows:
            dist_str = f"{r['距離(km)']} km" if r["距離(km)"] is not None else "不明"
            with container.expander(f"{r['施設名']}  |  {dist_str}", expanded=False):
                cols = st.columns([2, 2, 2, 2])
                cols[0].write(r["部屋"])
                cols[1].write(f"{r['開始']} - {r['終了']}")
                cols[2].write(r["料金"])
                cols[3].markdown(f"[予約する]({r['予約']})")

    render_results(paid, tab_paid)
    render_results(free, tab_free)
