import streamlit as st
import pydeck as pdk
import requests
import re
import json
import math
import os
from datetime import date, datetime, time as dtime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_URL = "https://city.nagano.nagano.machikagi-remote.jp"

# Rooms whose names contain any of these are not gymnasiums despite being tagged tag=1
NON_GYM_KEYWORDS = ["ホール", "会議室", "和室", "教室", "料理", "音楽", "調理", "講習", "実習", "図書", "ギャラリー"]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# 長野駅 — 距離の基準点
REF_LAT = 36.643809
REF_LON = 138.187750

# 地図に常に表示する駅（目印用、国土地理院で概算確認）— 信越本線の南北順
STATIONS = [
    ("篠ノ井駅", 36.580475, 138.143280),
    ("長野駅", 36.643809, 138.187750),
    ("北長野駅", 36.649345, 138.242966),
    ("豊野駅", 36.717712, 138.276352),
]

COLOR_PAID = [220, 40, 40]
COLOR_FREE = [46, 160, 67]
COLOR_STATION = [24, 119, 242]

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


def discover_rooms() -> list[dict]:
    if "rooms" in st.session_state:
        return st.session_state["rooms"]
    with st.spinner("施設・部屋情報を取得中..."):
        st.session_state["rooms"] = _fetch_rooms()
    return st.session_state["rooms"]


def _fetch_rooms() -> list[dict]:
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
                        "lat": coords[0] if coords else None,
                        "lon": coords[1] if coords else None,
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


def slot_in_time_range(start_str: str, end_str: str, f_start: dtime, f_end: dtime) -> bool:
    sh, sm = map(int, start_str.split(":"))
    eh, em = map(int, end_str.split(":"))
    return dtime(sh, sm) >= f_start and dtime(eh, em) <= f_end


# ── Page setup ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="長野 バドミントンコート空き検索",
    page_icon="https://fonts.gstatic.com/s/i/short-term/release/materialsymbolsoutlined/badminton/default/48px.svg",
    layout="wide",
)

st.markdown(
    """<link href="https://fonts.googleapis.com/css2?family=BIZ+UDPGothic:wght@400;700&display=swap" rel="stylesheet">
    <style>
    #MainMenu, header, footer {visibility: hidden;}
    [data-testid="stStatusWidget"], [data-testid="stAppViewerBadge"],
    .stAppDeployButton, div[class*="viewerBadge"] {
        display: none !important;
    }
    [data-testid="stApp"] * {
        font-family: 'BIZ UDPGothic' !important;
    }
    </style>""",
    unsafe_allow_html=True,
)

# ── Auth ─────────────────────────────────────────────────────────────────────

if not st.session_state.get("authenticated"):
    st.subheader("ログイン")
    with st.form("login_form"):
        username = st.text_input("ID")
        password = st.text_input("パスワード", type="password")
        if st.form_submit_button("ログイン", type="primary", use_container_width=True):
            if username == st.secrets["auth"]["username"] and password == st.secrets["auth"]["password"]:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("IDまたはパスワードが違います")
    st.stop()

# ── Main ─────────────────────────────────────────────────────────────────────

st.subheader("長野市バドミントンコート空き検索")
st.caption("距離基準：長野駅")

st.markdown(
    """<style>
    button[kind="secondary"] {
        background-color: #dc2828 !important;
        color: white !important;
        border-color: #dc2828 !important;
        transition: background-color 0.15s ease, border-color 0.15s ease;
    }
    button[kind="secondary"]:hover {
        background-color: #b81f1f !important;
        border-color: #b81f1f !important;
        color: white !important;
    }
    </style>""",
    unsafe_allow_html=True,
)

col_date, col_time, col_dist, col_search, col_refresh = st.columns(
    [2, 3, 2, 1, 1], vertical_alignment="bottom"
)
with col_date:
    selected_date = st.date_input(
        "日付",
        min_value=date.today(),
        value=date.today() + timedelta(days=1),
    )
with col_time:
    time_range = st.slider(
        "利用時間帯",
        min_value=dtime(6, 0),
        max_value=dtime(23, 0),
        value=(dtime(8, 0), dtime(22, 0)),
        step=timedelta(minutes=30),
        format="HH:mm",
    )
    filter_time_start, filter_time_end = time_range
with col_dist:
    max_dist_km = st.slider(
        "最大距離 (km)",
        min_value=0.0,
        max_value=30.0,
        value=20.0,
        step=0.5,
    )
with col_search:
    search = st.button("検索", type="primary", use_container_width=True)
with col_refresh:
    if st.button("施設一覧を更新", use_container_width=True):
        st.session_state.pop("rooms", None)
        st.rerun()

if search:
    date_str = selected_date.strftime("%Y-%m-%d")
    rooms = discover_rooms()

    if not rooms:
        st.error("施設情報を取得できませんでした。しばらくしてから再試行してください。")
        st.stop()

    rooms_in_range = [
        r for r in rooms
        if r["distance_km"] is None or r["distance_km"] <= max_dist_km
    ]

    results: list[dict] = []
    bar = st.progress(0, text="確認中...")

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
                results.append({
                    "施設名":   room["fname"],
                    "部屋":     room["rname"],
                    "開始":     s_start,
                    "終了":     s_end,
                    "料金":     slot.get("title", "").split("\n")[0],
                    "価格":     price,
                    "距離(km)": room["distance_km"],
                    "lat":      room["lat"],
                    "lon":      room["lon"],
                    "予約URL":  f"{BASE_URL}/rooms/{room['rid']}/reservation_calendar?date={date_str}",
                })
            bar.progress((i + 1) / len(rooms_in_range),
                         text=f"確認中 {i + 1}/{len(rooms_in_range)} 室")

    bar.empty()
    results.sort(key=sort_key)

    weekday_jp = ["月", "火", "水", "木", "金", "土", "日"]
    st.session_state["last_search"] = {
        "results": results,
        "date_str": date_str,
        "weekday": weekday_jp[selected_date.weekday()],
    }

# 検索ボタンを押していなくても、直前の検索結果はそのまま表示を続ける
# （日付・時間を変えただけで結果が消えるとUXが悪いため）
if "last_search" not in st.session_state:
    st.info("日付・条件を選んで「検索」を押してください")
    st.stop()

results = st.session_state["last_search"]["results"]
date_str = st.session_state["last_search"]["date_str"]
wd = st.session_state["last_search"]["weekday"]

if not results:
    st.warning(f"{date_str} は条件に合う空き枠が見つかりませんでした")
    st.stop()

paid = [r for r in results if r["価格"] is None or r["価格"] > 0]
free = [r for r in results if r["価格"] == 0]

# ── Map ──────────────────────────────────────────────────────────────────────

paid_fnames = {r["施設名"] for r in paid}
free_fnames = {r["施設名"] for r in free}

seen_fnames = set()
map_points = []
for r in results:
    fname = r["施設名"]
    if fname in seen_fnames or r["lat"] is None or r["lon"] is None:
        continue
    seen_fnames.add(fname)
    color = COLOR_PAID if fname in paid_fnames else COLOR_FREE
    map_points.append({
        "lat": r["lat"],
        "lon": r["lon"],
        "label": fname.split(" ", 1)[0],
        "color": color,
    })

if map_points:
    station_points = [
        {"lat": lat, "lon": lon, "label": name, "color": COLOR_STATION}
        for name, lat, lon in STATIONS
    ]
    all_points = map_points + station_points

    # deck.gl's TextLayer only pre-renders ASCII glyphs by default, so Japanese
    # labels render invisible unless the actual character set is passed explicitly.
    # Wrapping in quotes keeps pydeck from mangling it into a data-field accessor.
    label_chars = "".join(sorted({c for p in all_points for c in p["label"]}))
    quoted_char_set = "'" + label_chars + "'"
    quoted_font_family = '"' + "'BIZ UDPGothic'" + '"'

    line_layer = pdk.Layer(
        "PathLayer",
        data=[{"path": [[lon, lat] for _, lat, lon in STATIONS]}],
        get_path="path",
        get_color=COLOR_STATION,
        get_width=3,
        width_min_pixels=2,
    )
    badge_layer = pdk.Layer(
        "TextLayer",
        data=all_points,
        get_position="[lon, lat]",
        get_text="label",
        get_size=12.5,
        get_color=[255, 255, 255],
        get_alignment_baseline="'center'",
        character_set=quoted_char_set,
        font_family=quoted_font_family,
        font_weight=700,
        background=True,
        get_background_color="color",
        background_padding=[7.5, 5],
        pickable=True,
    )
    view_state = pdk.ViewState(
        latitude=sum(p["lat"] for p in all_points) / len(all_points),
        longitude=sum(p["lon"] for p in all_points) / len(all_points),
        zoom=11,
    )
    st.markdown(
        """<style>
        .st-key-map_box {
            position: relative;
        }
        .st-key-map_box [data-testid="stElementToolbar"] {
            display: none !important;
        }
        .map-date-badge {
            position: absolute;
            top: 16px;
            left: 16px;
            z-index: 1000;
            background-color: #3a3a3a;
            color: white;
            padding: 4px 10px;
            font-weight: 700;
            font-size: 0.85rem;
        }
        </style>""",
        unsafe_allow_html=True,
    )
    with st.container(key="map_box"):
        st.markdown(f'<div class="map-date-badge">{date_str} （{wd}）</div>', unsafe_allow_html=True)
        st.pydeck_chart(pdk.Deck(
            layers=[line_layer, badge_layer],
            initial_view_state=view_state,
            map_style=pdk.map_styles.LIGHT_NO_LABELS,
        ), height=781)

    def legend_swatch(color: list[int], label: str) -> str:
        r, g, b = color
        return (
            f'<span style="display:inline-block;width:16px;height:10px;border-radius:2px;'
            f'background-color:rgb({r},{g},{b});margin-right:4px;"></span>'
            f'<span style="margin-right:16px;">{label}</span>'
        )

    st.markdown(
        f'<div style="font-size:0.8rem;color:gray;">'
        f'{legend_swatch(COLOR_PAID, "有料")}'
        f'{legend_swatch(COLOR_FREE, "無料")}'
        f'{legend_swatch(COLOR_STATION, "駅")}'
        f'</div>',
        unsafe_allow_html=True,
    )

st.divider()

# ── Results ──────────────────────────────────────────────────────────────────

def render_results(rows: list[dict], container) -> None:
    if not rows:
        container.info("該当なし")
        return
    prev = None
    for r in rows:
        if r["施設名"] != prev:
            dist = r["距離(km)"]
            dist_str = f"{dist} km" if dist is not None else "距離不明"
            container.markdown(f"**{r['施設名']}** &nbsp; `{dist_str}`")
            prev = r["施設名"]
        c1, c2, c3, c4 = container.columns([3, 2, 2, 1])
        c1.write(r["部屋"])
        c2.write(f"{r['開始']} – {r['終了']}")
        c3.write(r["料金"])
        c4.markdown(f"[予約]({r['予約URL']})")
    container.write("")

tab_paid, tab_free = st.tabs([f"有料 ({len(paid)}件)", f"無料 ({len(free)}件)"])
render_results(paid, tab_paid)
render_results(free, tab_free)
