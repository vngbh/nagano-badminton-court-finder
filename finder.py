import streamlit as st
import requests
import re
import json
import math
import os
from datetime import date, datetime, time as dtime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_URL = "https://city.nagano.nagano.machikagi-remote.jp"

# Rooms whose names contain any of these are not gymnasiums despite being tagged tag=1
NON_GYM_KEYWORDS = ["ホール", "会議室", "和室", "教室", "料理", "音楽", "調理", "講習", "実習", "図書"]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# 信州大学工学部（長野市若里4丁目17-1）— 国土地理院で確認済み
REF_LAT = 36.63179
REF_LON = 138.187378

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
    """Geocode a Japanese address using 国土地理院 (GSI Japan) — no API key needed."""
    if address in cache and cache[address] is not None:
        return cache[address]
    try:
        resp = requests.get(
            "https://msearch.gsi.go.jp/address-search/AddressSearch",
            params={"q": address},
            timeout=10,
        )
        results = resp.json()
        if results:
            lon, lat = results[0]["geometry"]["coordinates"]
            coords = (float(lat), float(lon))
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

            # Geocode via 国土地理院
            coords = None
            if address:
                coords = geocode(address, coords_cache)

            distance_km = (
                haversine(REF_LAT, REF_LON, coords[0], coords[1])
                if coords
                else None
            )

            # Get rooms for this facility — tag=1 filters to 体育館 rooms only
            resp2 = requests.get(
                f"{BASE_URL}/rooms",
                params={"facility_id": fid, "tag": "1"},
                headers=HEADERS,
                timeout=15,
            )
            room_matches = re.findall(
                r'<a class="trans" href="/rooms/(\d+)">(.*?)</a>', resp2.text
            )
            for rid, rname in room_matches:
                rname = rname.strip()
                if any(kw in rname for kw in NON_GYM_KEYWORDS):
                    continue
                rooms.append(
                    {
                        "fid": fid,
                        "fname": fname,
                        "rid": rid,
                        "rname": rname,
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


# ── helpers ─────────────────────────────────────────────────────────────────

def slot_in_time_range(start_str: str, end_str: str, f_start: dtime, f_end: dtime) -> bool:
    """True when the slot [start_str, end_str] lies completely within [f_start, f_end]."""
    sh, sm = map(int, start_str.split(":"))
    eh, em = map(int, end_str.split(":"))
    return dtime(sh, sm) >= f_start and dtime(eh, em) <= f_end


# ── UI ──────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="長野 バドミントンコート空き検索")
st.title("長野市 バドミントンコート 空き検索")
st.caption("基準: 信州大学工学部 / データ取得元: 長野市 施設案内予約システム（ログイン不要）")

# ── Date & refresh ───────────────────────────────────────────────────────────
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

# ── Filters ──────────────────────────────────────────────────────────────────
with st.expander("フィルター", expanded=True):
    f_col1, f_col2 = st.columns(2)

    with f_col1:
        time_range = st.slider(
            "利用時間帯",
            min_value=dtime(6, 0),
            max_value=dtime(23, 0),
            value=(dtime(8, 0), dtime(22, 0)),
            step=timedelta(minutes=30),
            format="HH:mm",
        )
        filter_time_start, filter_time_end = time_range

    with f_col2:
        max_dist_km = st.slider(
            "最大距離 (km)  ※左端は常に 0.0 km",
            min_value=0.0,
            max_value=30.0,
            value=20.0,
            step=0.5,
        )

# ── Search ───────────────────────────────────────────────────────────────────
if st.button("空きを検索", type="primary", use_container_width=True):
    date_str = selected_date.strftime("%Y-%m-%d")
    rooms = discover_rooms()

    if not rooms:
        st.error("施設情報を取得できませんでした。しばらくしてから再試行してください。")
        st.stop()

    # Pre-filter rooms by distance before fetching slots
    rooms_in_range = [
        r for r in rooms
        if r["distance_km"] is None or r["distance_km"] <= max_dist_km
    ]

    results: list[dict] = []
    bar = st.progress(0)
    status = st.empty()

    with ThreadPoolExecutor(max_workers=15) as executor:
        futures = {executor.submit(fetch_slots, r, date_str): r for r in rooms_in_range}
        for i, future in enumerate(as_completed(futures)):
            room = futures[future]
            for slot in future.result():
                s_start = slot["start"][11:16]
                s_end   = slot["end"][11:16]
                if not slot_in_time_range(s_start, s_end, filter_time_start, filter_time_end):
                    continue
                price = extract_price(slot.get("title", ""))
                results.append(
                    {
                        "施設名": room["fname"],
                        "部屋": room["rname"],
                        "開始": s_start,
                        "終了": s_end,
                        "料金": slot.get("title", "").split("\n")[0],
                        "価格": price,
                        "距離(km)": room["distance_km"],
                        "予約": f"{BASE_URL}/rooms/{room['rid']}/reservation_calendar?date={date_str}",
                    }
                )
            bar.progress((i + 1) / len(rooms_in_range))
            status.text(f"確認中 {i + 1}/{len(rooms_in_range)} 室")

    bar.empty()
    status.empty()

    if not results:
        st.warning(f"{date_str} は条件に合う空き枠が見つかりませんでした")
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
            dist_str = f"{r['距離(km)']} km" if r["距離(km)"] is not None else "距離不明"
            with container.expander(f"{r['施設名']}  |  {dist_str}", expanded=False):
                cols = st.columns([2, 2, 2, 2])
                cols[0].write(r["部屋"])
                cols[1].write(f"{r['開始']} - {r['終了']}")
                cols[2].write(r["料金"])
                cols[3].markdown(f"[予約する]({r['予約']})")

    render_results(paid, tab_paid)
    render_results(free, tab_free)
