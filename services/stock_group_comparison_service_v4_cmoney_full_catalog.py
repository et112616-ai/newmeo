from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pandas as pd

from services.stock_service import get_history, normalize_stock_input


STOCK_GROUP_COMPARISON_VERSION = (
    "2026-07-29-v4-CMONEY-FULL-CATALOG-SNAPSHOT"
)
STOCK_CONCEPT_PEER_VERSION = STOCK_GROUP_COMPARISON_VERSION
SIMILARITY_WINDOW_DAYS = max(
    40,
    min(
        int(os.getenv("STOCK_PEER_WINDOW_DAYS", "60")),
        120,
    ),
)
TOP_PEER_LIMIT = max(
    1,
    min(
        int(os.getenv("STOCK_PEER_TOP_LIMIT", "3")),
        5,
    ),
)
MAX_CANDIDATES = max(
    3,
    min(
        int(os.getenv("STOCK_PEER_MAX_CANDIDATES", "8")),
        12,
    ),
)
CACHE_TTL_SECONDS = max(
    60,
    int(os.getenv("STOCK_PEER_CACHE_TTL_SECONDS", "900")),
)


# 概念族群主目錄以 CMoney「股票概念股分類總覽」為分類參考，
# 並保留本機快照，避免 LINE 查詢時依賴外站而逾時。
# 這不是交易所的單一產業分類；同一檔股票可同時屬於多個概念。
CONCEPT_GROUPS: dict[str, dict[str, str]] = {
    "軍工": {
        "1810": "和成",
        "2634": "漢翔",
        "2645": "長榮航太",
        "4571": "鈞興-KY",
        "6753": "龍德造船",
        "8033": "雷虎",
    },
    "回收": {
        "9955": "佳龍",
        "8390": "金益鼎",
        "1785": "光洋科",
    },
    "CCL": {
        "6213": "聯茂",
        "6274": "台燿",
        "2383": "台光電",
    },
    "SM": {
        "1326": "台化",
        "1310": "台苯",
        "1312": "國喬",
    },
    "PVC": {
        "1301": "台塑",
        "1305": "華夏",
    },
    "水泥": {
        "1101": "台泥",
        "1102": "亞泥",
        "1103": "嘉泥",
        "1104": "環泥",
        "1108": "幸福",
        "1109": "信大",
        "1110": "東泥",
        "2504": "國產",
    },
    "散熱": {
        "3324": "雙鴻",
        "3653": "健策",
        "3338": "泰碩",
        "3017": "奇鋐",
        "6230": "尼得科超眾",
        "8996": "高力",
    },
    "散熱風扇": {
        "2421": "建準",
        "6591": "動力-KY",
    },
    "食品": {
        "1215": "卜蜂",
        "1210": "大成",
        "1201": "味全",
    },
    "記憶體-DRAM模組": {
        "8271": "宇瞻",
        "3260": "威剛",
        "4967": "十銓",
    },
    "記憶體-原廠": {
        "2337": "旺宏",
        "2344": "華邦電",
        "2408": "南亞科",
    },
    "記憶體-NAND控制": {
        "8299": "群聯",
    },
    "製鞋": {
        "9904": "寶成",
        "9910": "豐泰",
        "9802": "鈺齊-KY",
    },
    "輪胎": {
        "2105": "正新",
        "2106": "建大",
    },
    "PA": {
        "3105": "穩懋",
        "8086": "宏捷科",
        "2455": "全新",
    },
    "矽晶圓": {
        "6488": "環球晶",
        "3532": "台勝科",
        "6182": "合晶",
    },
    "安控": {
        "3454": "晶睿",
        "3356": "奇偶",
    },
    "LTCC": {
        "3152": "璟德",
        "6271": "同欣電",
    },
    "COF": {
        "6147": "頎邦",
        "6552": "易華電",
    },
    "MacBook鍵盤": {
        "2387": "精元",
        "4935": "茂林-KY",
        "5215": "科嘉-KY",
    },
    "偏光片": {
        "8215": "明基材",
        "4960": "誠美材",
    },
    "3C通路": {
        "2430": "燦坤",
        "6281": "全國電",
    },
    "工紙": {
        "1904": "正隆",
        "1907": "永豐餘",
        "1909": "榮成",
    },
    "面板": {
        "3481": "群創",
        "2409": "友達",
    },
    "CIS": {
        "8249": "菱光",
        "4974": "亞泰",
        "6271": "同欣電",
        "3530": "晶相光",
    },
    "自行車": {
        "9921": "巨大",
        "9914": "美利達",
        "5306": "桂盟",
    },
    "超商": {
        "2912": "統一超",
        "5903": "全家",
    },
    "晶圓代工": {
        "2330": "台積電",
        "2303": "聯電",
        "6770": "力積電",
        "5347": "世界",
    },
    "健身": {
        "8462": "柏文",
        "1736": "喬山",
        "1598": "岱宇",
    },
    "MCU": {
        "6202": "盛群",
        "4919": "新唐",
        "5471": "松翰",
        "2436": "偉詮電",
        "2458": "義隆",
        "6457": "紘康",
        "6494": "九齊",
        "6243": "迅杰",
        "4952": "凌通",
        "5236": "凌陽創新",
        "3122": "笙泉",
        "3228": "金麗科",
        "4945": "陞達科技",
        "3438": "類比科",
    },
    "ABF載板": {
        "3037": "欣興",
        "3189": "景碩",
        "8046": "南電",
    },
    "光通訊": {
        "4977": "眾達-KY",
        "3081": "聯亞",
        "6426": "統新",
    },
    "紡織成衣": {
        "1434": "福懋",
        "4426": "利勤",
        "1477": "聚陽",
        "1476": "儒鴻",
        "4401": "東隆興",
    },
    "高爾夫球": {
        "8924": "大田",
        "8928": "鉅明",
        "8938": "明安",
        "6670": "復盛應用",
    },
    "被動元件": {
        "2327": "國巨",
        "2492": "華新科",
        "2375": "凱美",
        "3026": "禾伸堂",
        "6173": "信昌電",
    },
    "IC測試／載板": {
        "6510": "精測",
        "6683": "雍智",
        "6141": "柏承",
    },
    "MOSFET": {
        "5299": "杰力",
        "6435": "大中",
        "3317": "尼克森",
        "8261": "富鼎",
    },
    "隱形眼鏡": {
        "1565": "精華",
        "8406": "金可-KY",
        "6491": "晶碩",
    },
    "觸控": {
        "6456": "GIS-KY",
        "3673": "TPK-KY",
    },
    "銅箔": {
        "8358": "金居",
        "4989": "榮科",
    },
    "保護元件": {
        "2428": "興勤",
        "6224": "聚鼎",
        "6642": "富致",
    },
    "導線架": {
        "2351": "順德",
        "5285": "界霖",
        "6548": "長科*",
    },
    "車用二極體": {
        "8255": "朋程",
        "5425": "台半",
        "3675": "德微",
        "2481": "強茂",
    },
    "高速傳輸": {
        "5269": "祥碩",
        "6756": "威鋒電子",
        "6104": "創惟",
        "3588": "通嘉",
        "2436": "偉詮電",
        "5351": "鈺創",
        "6233": "旺玖",
    },
    "低軌衛星": {
        "3505": "昇貿",
        "5309": "系統電",
        "3178": "公準",
        "6412": "群電",
        "2312": "金寶",
        "2313": "華通",
        "2314": "台揚",
        "2327": "國巨",
        "2383": "台光電",
        "2454": "聯發科",
        "3105": "穩懋",
        "3491": "昇達科",
        "6271": "同欣電",
        "6285": "啟碁",
        "6510": "精測",
        "6282": "康舒",
    },
    "元宇宙-VR零件": {
        "5236": "凌陽創新",
        "3504": "揚明光",
        "2344": "華邦電",
        "2484": "希華",
        "3217": "優群",
        "2401": "凌陽",
        "2426": "鼎元",
        "3508": "位速",
        "2340": "台亞",
    },
    "元宇宙-VR成品周邊": {
        "2498": "宏達電",
        "3019": "亞光",
        "2458": "義隆",
    },
    "元宇宙-AR成像": {
        "3227": "原相",
        "5351": "鈺創",
        "8086": "宏捷科",
        "2357": "華碩",
    },
    "工業4.0": {
        "2395": "研華",
        "6166": "凌華",
        "3005": "神基",
        "6414": "樺漢",
        "4916": "事欣科",
        "8114": "振樺電",
        "6206": "飛捷",
    },
    "台積電供應鏈-電力工程": {
        "1514": "亞力",
    },
    "台積電供應鏈-無塵室": {
        "2404": "漢唐",
        "5536": "聖暉*",
        "6613": "朋億*",
        "6139": "亞翔",
        "6196": "帆宣",
        "8383": "千附",
        "3402": "漢科",
    },
    "台積電供應鏈-探針": {
        "6217": "中探針",
        "6223": "旺矽",
    },
    "台積電供應鏈-光罩": {
        "2338": "光罩",
        "3680": "家登",
    },
    "台積電供應鏈-矽晶圓": {
        "6488": "環球晶",
        "3532": "台勝科",
        "6182": "合晶",
    },
    "台積電供應鏈-IC封測": {
        "3374": "精材",
        "3711": "日月光投控",
        "3264": "欣銓",
    },
    "台積電供應鏈-前端測試": {
        "6510": "精測",
        "6683": "雍智",
    },
    "台積電供應鏈-檢測": {
        "3289": "宜特",
        "3587": "閎康",
    },
    "台積電供應鏈-材料化學": {
        "1711": "永光",
        "1717": "長興",
        "5344": "崇越",
        "1773": "勝一",
        "8091": "翔名",
        "4755": "三福化",
        "1785": "光洋科",
        "3010": "華立",
    },
    "台積電供應鏈-設備製程": {
        "3551": "世禾",
        "3413": "京鼎",
        "6532": "瑞耘",
        "6640": "均華",
        "2464": "盟立",
        "2360": "致茂",
    },
    "PCB": {
        "2313": "華通",
        "2368": "金像電",
        "3037": "欣興",
        "3044": "健鼎",
        "3189": "景碩",
        "4958": "臻鼎-KY",
        "6213": "聯茂",
        "6274": "台燿",
    },
    "AI伺服器": {
        "2317": "鴻海",
        "2324": "仁寶",
        "2382": "廣達",
        "3231": "緯創",
        "6669": "緯穎",
    },
    "IC設計": {
        "2379": "瑞昱",
        "2454": "聯發科",
        "3034": "聯詠",
        "3443": "創意",
        "3661": "世芯-KY",
        "6531": "愛普*",
    },
    "貨櫃航運": {
        "2603": "長榮",
        "2609": "陽明",
        "2615": "萬海",
    },
    "金控": {
        "2881": "富邦金",
        "2882": "國泰金",
        "2884": "玉山金",
        "2885": "元大金",
        "2886": "兆豐金",
        "2887": "台新新光金",
        "2891": "中信金",
        "2892": "第一金",
    },
    "網通": {
        "2345": "智邦",
        "2419": "仲琦",
        "3045": "台灣大",
        "3596": "智易",
        "3704": "合勤控",
        "6285": "啟碁",
    },
}


def _merge_concept_groups(*group_names: str) -> dict[str, str]:
    merged: dict[str, str] = {}
    for group_name in group_names:
        merged.update(CONCEPT_GROUPS.get(group_name, {}))
    return merged


# 大類與子類並存，讓股票可同時進行精細及跨子類比較。
CONCEPT_GROUPS["記憶體"] = _merge_concept_groups(
    "記憶體-DRAM模組",
    "記憶體-原廠",
    "記憶體-NAND控制",
)
CONCEPT_GROUPS["元宇宙"] = _merge_concept_groups(
    "元宇宙-VR零件",
    "元宇宙-VR成品周邊",
    "元宇宙-AR成像",
)
CONCEPT_GROUPS["台積電供應鏈"] = _merge_concept_groups(
    "台積電供應鏈-電力工程",
    "台積電供應鏈-無塵室",
    "台積電供應鏈-探針",
    "台積電供應鏈-光罩",
    "台積電供應鏈-矽晶圓",
    "台積電供應鏈-IC封測",
    "台積電供應鏈-前端測試",
    "台積電供應鏈-檢測",
    "台積電供應鏈-材料化學",
    "台積電供應鏈-設備製程",
)

CATALOG_SOURCE_NAME = "CMoney 股票概念股分類總覽"
CATALOG_SOURCE_URL = "https://www.cmoney.tw/forum/concept"
DEFAULT_CATALOG_PATH = Path(__file__).resolve().with_name(
    "cmoney_concept_catalog_v2.json"
)
DEFAULT_OVERRIDE_PATH = Path(__file__).resolve().with_name(
    "stock_concept_manual_overrides_v1.json"
)


def _load_cmoney_catalog() -> dict[str, Any]:
    configured = str(
        os.getenv("STOCK_CONCEPT_CATALOG_PATH", "")
    ).strip()
    path = Path(configured) if configured else DEFAULT_CATALOG_PATH
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except FileNotFoundError:
        print(
            "DEBUG stock_group_comparison | catalog snapshot missing",
            "| path =", str(path),
            "| fallback_groups =", len(CONCEPT_GROUPS),
            flush=True,
        )
        return {}
    except Exception as exc:
        print(
            "DEBUG stock_group_comparison | catalog load failed",
            "| path =", str(path),
            "| error =", repr(exc),
            flush=True,
        )
        return {}


def _load_manual_overrides() -> dict[str, Any]:
    configured = str(
        os.getenv("STOCK_CONCEPT_OVERRIDE_PATH", "")
    ).strip()
    path = Path(configured) if configured else DEFAULT_OVERRIDE_PATH
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(
            "DEBUG stock_group_comparison | override load failed",
            "| path =", str(path),
            "| error =", repr(exc),
            flush=True,
        )
        return {}


_CMONEY_CATALOG = _load_cmoney_catalog()
_CMONEY_GROUPS = _CMONEY_CATALOG.get("groups") or {}
if isinstance(_CMONEY_GROUPS, dict) and _CMONEY_GROUPS:
    _snapshot_groups: dict[str, dict[str, str]] = {}
    for _group_name, _group_payload in _CMONEY_GROUPS.items():
        if not isinstance(_group_payload, dict):
            continue
        _raw_members = (
            _group_payload.get("members")
            if "members" in _group_payload
            else _group_payload
        )
        if not isinstance(_raw_members, dict):
            continue
        _snapshot_groups[str(_group_name).strip()] = {
            str(_stock_id).strip(): str(
                _stock_name_value or _stock_id
            ).strip()
            for _stock_id, _stock_name_value in _raw_members.items()
            if str(_stock_id).strip()
        }
    if _snapshot_groups:
        CONCEPT_GROUPS.clear()
        CONCEPT_GROUPS.update(_snapshot_groups)

_MANUAL_OVERRIDES = _load_manual_overrides()
for _group_name, _changes in (
    _MANUAL_OVERRIDES.get("groups") or {}
).items():
    if not isinstance(_changes, dict):
        continue
    _members = CONCEPT_GROUPS.setdefault(str(_group_name), {})
    for _stock_id in _changes.get("remove") or []:
        _members.pop(str(_stock_id).strip(), None)
    for _stock_id, _stock_name_value in (
        _changes.get("add") or {}
    ).items():
        _normalized_stock_id = str(_stock_id).strip()
        if _normalized_stock_id:
            _members[_normalized_stock_id] = str(
                _stock_name_value or _normalized_stock_id
            ).strip()

# 舊版子分類仍存在時才同步回大類；完整 CMoney 快照不額外製造空族群。
_legacy_memory = _merge_concept_groups(
        "記憶體-DRAM模組",
        "記憶體-原廠",
        "記憶體-NAND控制",
)
if _legacy_memory:
    CONCEPT_GROUPS.setdefault("記憶體", {}).update(_legacy_memory)

_legacy_metaverse = _merge_concept_groups(
        "元宇宙-VR零件",
        "元宇宙-VR成品周邊",
        "元宇宙-AR成像",
)
if _legacy_metaverse:
    CONCEPT_GROUPS.setdefault("元宇宙", {}).update(_legacy_metaverse)

_legacy_tsmc_supply_chain = _merge_concept_groups(
        "台積電供應鏈-電力工程",
        "台積電供應鏈-無塵室",
        "台積電供應鏈-探針",
        "台積電供應鏈-光罩",
        "台積電供應鏈-矽晶圓",
        "台積電供應鏈-IC封測",
        "台積電供應鏈-前端測試",
        "台積電供應鏈-檢測",
        "台積電供應鏈-材料化學",
        "台積電供應鏈-設備製程",
)
if _legacy_tsmc_supply_chain:
    CONCEPT_GROUPS.setdefault("台積電供應鏈", {}).update(
        _legacy_tsmc_supply_chain
    )

# 人工確認的核心同概念配對只影響優先順序，其餘仍按歷史相似度排序。
CONCEPT_ANCHOR_PEERS: dict[str, list[str]] = {
    "1810": ["8033"],
    "8033": ["1810"],
    "2327": ["2492"],
    "2492": ["2327"],
}
for _stock_id, _peer_ids in (
    _MANUAL_OVERRIDES.get("anchor_peers") or {}
).items():
    if isinstance(_peer_ids, list):
        CONCEPT_ANCHOR_PEERS[str(_stock_id).strip()] = [
            str(_peer_id).strip()
            for _peer_id in _peer_ids
            if str(_peer_id).strip()
        ]

CATALOG_UPDATED_AT = str(
    _MANUAL_OVERRIDES.get("updated_at")
    or _CMONEY_CATALOG.get("updated_at")
    or "2026-07-29"
)
CATALOG_MAINTENANCE_MODE = (
    "CMoney完整離線快照＋人工不定期覆寫"
)
CATALOG_COMPLETENESS = (
    _CMONEY_CATALOG.get("completeness") or {}
)
CATALOG_GROUP_COUNT = len(CONCEPT_GROUPS)
CATALOG_STOCK_COUNT = len(
    {
        stock_id
        for members in CONCEPT_GROUPS.values()
        for stock_id in members
    }
)


_CACHE_LOCK = threading.Lock()
_RESULT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _debug(*args: Any) -> None:
    print("DEBUG stock_group_comparison |", *args, flush=True)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except Exception:
        return default


def _concepts_for(stock_id: str) -> list[str]:
    return [
        concept
        for concept, members in CONCEPT_GROUPS.items()
        if stock_id in members
    ]


def _stock_name(stock_id: str) -> str:
    for members in CONCEPT_GROUPS.values():
        if stock_id in members:
            known_name = str(members[stock_id] or "").strip()
            if known_name and known_name != stock_id:
                return known_name
    try:
        meta = normalize_stock_input(stock_id)
        if isinstance(meta, dict):
            normalized_name = str(
                meta.get("stock_name")
                or meta.get("name")
                or ""
            ).strip()
        else:
            normalized_name = str(
                getattr(meta, "stock_name", "")
                or getattr(meta, "name", "")
            ).strip()
        if normalized_name:
            return normalized_name
    except Exception:
        pass
    return stock_id


def _is_supported_comparison_stock(stock_id: str) -> bool:
    # 目前既有日線服務以台灣上市櫃四位數股票代碼最穩定；
    # 快照仍完整保留 TDR 等其他代碼，但不讓它們占用候選額度。
    return bool(re.fullmatch(r"\d{4}", str(stock_id or "")))


def _daily_frame(stock_id: str) -> pd.DataFrame:
    meta = normalize_stock_input(stock_id)
    history_result = get_history(meta, "D")
    history = (
        history_result[0]
        if isinstance(history_result, tuple)
        else history_result
    )
    if history is None or getattr(history, "empty", True):
        return pd.DataFrame()

    frame = history.copy()
    if "Close" not in frame.columns:
        return pd.DataFrame()
    frame["Close"] = pd.to_numeric(frame["Close"], errors="coerce")
    if "Volume" not in frame.columns:
        frame["Volume"] = 0.0
    frame["Volume"] = pd.to_numeric(
        frame["Volume"],
        errors="coerce",
    ).fillna(0.0)
    try:
        frame.index = pd.to_datetime(frame.index).tz_localize(None)
    except Exception:
        frame.index = pd.to_datetime(frame.index)
    frame = frame.dropna(subset=["Close"])
    frame = frame[frame["Close"] > 0]
    frame = frame[~frame.index.duplicated(keep="last")]
    return frame.sort_index().tail(SIMILARITY_WINDOW_DAYS + 25)


def _log_volume_change(series: pd.Series) -> pd.Series:
    cleaned = pd.to_numeric(series, errors="coerce").fillna(0.0)
    logged = (cleaned.clip(lower=0.0) + 1.0).map(math.log)
    return logged.diff()


def _period_return(series: pd.Series, periods: int) -> float:
    if len(series) <= periods:
        return 0.0
    current = _safe_float(series.iloc[-1])
    previous = _safe_float(series.iloc[-1 - periods])
    if current <= 0 or previous <= 0:
        return 0.0
    return (current / previous - 1.0) * 100.0


def _calculate_pair(
    target_id: str,
    target_name: str,
    target: pd.DataFrame,
    peer_id: str,
    peer_name: str,
    peer: pd.DataFrame,
    shared_concepts: list[str],
) -> dict[str, Any] | None:
    joined = (
        target[["Close", "Volume"]]
        .rename(
            columns={
                "Close": "target_close",
                "Volume": "target_volume",
            }
        )
        .join(
            peer[["Close", "Volume"]].rename(
                columns={
                    "Close": "peer_close",
                    "Volume": "peer_volume",
                }
            ),
            how="inner",
        )
        .dropna(subset=["target_close", "peer_close"])
        .tail(SIMILARITY_WINDOW_DAYS + 1)
    )
    if len(joined) < 31:
        return None

    target_return = (
        joined["target_close"]
        .pct_change(fill_method=None)
        .clip(-0.2, 0.2)
    )
    peer_return = (
        joined["peer_close"]
        .pct_change(fill_method=None)
        .clip(-0.2, 0.2)
    )
    returns = pd.concat(
        [target_return, peer_return],
        axis=1,
        keys=["target", "peer"],
    ).dropna()
    if len(returns) < 30:
        return None

    correlation = _safe_float(
        returns["target"].corr(returns["peer"])
    )
    direction_agreement = _safe_float(
        (
            (returns["target"] * returns["peer"] > 0)
            | (
                (returns["target"].abs() < 1e-12)
                & (returns["peer"].abs() < 1e-12)
            )
        ).mean()
    )
    target_volatility = _safe_float(returns["target"].std())
    peer_volatility = _safe_float(returns["peer"].std())
    volatility_similarity = (
        min(target_volatility, peer_volatility)
        / max(target_volatility, peer_volatility)
        if target_volatility > 0 and peer_volatility > 0
        else 0.0
    )

    volume_changes = pd.concat(
        [
            _log_volume_change(joined["target_volume"]),
            _log_volume_change(joined["peer_volume"]),
        ],
        axis=1,
        keys=["target", "peer"],
    ).replace([math.inf, -math.inf], pd.NA).dropna()
    volume_correlation = (
        _safe_float(
            volume_changes["target"].corr(volume_changes["peer"])
        )
        if len(volume_changes) >= 20
        else 0.0
    )

    similarity = 100.0 * (
        0.50 * max(0.0, min(1.0, correlation))
        + 0.25 * max(0.0, min(1.0, direction_agreement))
        + 0.15 * max(0.0, min(1.0, volatility_similarity))
        + 0.10 * max(0.0, min(1.0, volume_correlation))
    )

    ratio = (
        joined["target_close"] / joined["peer_close"]
    ).replace([math.inf, -math.inf], pd.NA).dropna()
    if len(ratio) < 30:
        return None
    ratio_mean = _safe_float(ratio.mean())
    ratio_std = _safe_float(ratio.std())
    ratio_current = _safe_float(ratio.iloc[-1])
    ratio_zscore = (
        (ratio_current - ratio_mean) / ratio_std
        if ratio_std > 0
        else 0.0
    )
    ratio_deviation_pct = (
        (ratio_current / ratio_mean - 1.0) * 100.0
        if ratio_mean > 0
        else 0.0
    )

    target_return_5 = _period_return(joined["target_close"], 5)
    peer_return_5 = _period_return(joined["peer_close"], 5)
    target_return_20 = _period_return(joined["target_close"], 20)
    peer_return_20 = _period_return(joined["peer_close"], 20)
    relative_5 = target_return_5 - peer_return_5
    relative_20 = target_return_20 - peer_return_20

    if ratio_zscore >= 0.75:
        status = f"{target_name}相對偏強"
    elif ratio_zscore <= -0.75:
        status = f"{peer_name}相對偏強"
    else:
        status = "比值接近均值"

    chart_rows = [
        {
            "date": pd.Timestamp(index).strftime("%Y-%m-%d"),
            "ratio": round(_safe_float(value), 8),
        }
        for index, value in ratio.items()
    ]
    return {
        "target_id": target_id,
        "target_name": target_name,
        "peer_id": peer_id,
        "peer_name": peer_name,
        "concepts": shared_concepts,
        "primary_concept": shared_concepts[0] if shared_concepts else "",
        "similarity_pct": round(similarity, 2),
        "return_correlation": round(correlation, 6),
        "direction_agreement_pct": round(direction_agreement * 100.0, 2),
        "volatility_similarity_pct": round(
            volatility_similarity * 100.0,
            2,
        ),
        "volume_correlation": round(volume_correlation, 6),
        "ratio_current": round(ratio_current, 8),
        "ratio_mean": round(ratio_mean, 8),
        "ratio_deviation_pct": round(ratio_deviation_pct, 4),
        "ratio_zscore": round(ratio_zscore, 4),
        "target_return_5_pct": round(target_return_5, 4),
        "peer_return_5_pct": round(peer_return_5, 4),
        "relative_strength_5_pct": round(relative_5, 4),
        "target_return_20_pct": round(target_return_20, 4),
        "peer_return_20_pct": round(peer_return_20, 4),
        "relative_strength_20_pct": round(relative_20, 4),
        "status": status,
        "sample_days": len(returns),
        "data_date": pd.Timestamp(joined.index[-1]).strftime("%Y-%m-%d"),
        "chart_rows": chart_rows,
        "chart_url": "",
    }


def _publish_ratio_chart(comparison: dict[str, Any]) -> str:
    rows = comparison.get("chart_rows") or []
    if len(rows) < 2:
        return ""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt

        from services.upload_service import publish_figure

        frame = pd.DataFrame(rows)
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame["ratio"] = pd.to_numeric(
            frame["ratio"],
            errors="coerce",
        )
        frame = frame.dropna()
        if len(frame) < 2:
            return ""

        mean_value = _safe_float(comparison.get("ratio_mean"))
        std_value = _safe_float(frame["ratio"].std())
        current_value = _safe_float(frame["ratio"].iloc[-1])

        fig, ax = plt.subplots(
            figsize=(7.2, 3.2),
            dpi=120,
            facecolor="white",
        )
        ax.set_facecolor("#FAFAFA")
        if std_value > 0:
            ax.fill_between(
                frame["date"],
                mean_value - std_value,
                mean_value + std_value,
                color="#EDE9FE",
                alpha=0.75,
                label="均值±1σ",
            )
        ax.plot(
            frame["date"],
            frame["ratio"],
            color="#252525",
            linewidth=1.8,
            label="股價比值",
        )
        ax.axhline(
            mean_value,
            color="#D4A400",
            linestyle="--",
            linewidth=1.2,
            label="60日均值",
        )
        ax.scatter(
            frame["date"].iloc[-1],
            current_value,
            color="#7C3AED",
            s=34,
            zorder=4,
        )
        ax.annotate(
            f"{current_value:.3f}",
            (
                frame["date"].iloc[-1],
                current_value,
            ),
            xytext=(-4, 8),
            textcoords="offset points",
            ha="right",
            fontsize=9,
            color="#6D28D9",
        )
        ax.grid(
            True,
            color="#D1D5DB",
            linewidth=0.6,
            alpha=0.55,
        )
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=6))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
        ax.tick_params(axis="both", labelsize=9, colors="#4B5563")
        ax.set_ylabel("股價比值", fontsize=9, color="#4B5563")
        for spine in ax.spines.values():
            spine.set_color("#D1D5DB")
        ax.legend(
            loc="upper left",
            fontsize=8,
            frameon=False,
            ncol=3,
        )
        fig.tight_layout(pad=0.8)
        name = (
            f"{comparison.get('target_id')}_"
            f"{comparison.get('peer_id')}_peer_ratio"
        )
        return str(publish_figure(fig, name) or "")
    except Exception as exc:
        _debug(
            "ratio chart failed",
            "| target =", comparison.get("target_id"),
            "| peer =", comparison.get("peer_id"),
            "| error =", repr(exc),
        )
        return ""


def build_stock_concept_peer_comparison(
    stock_id: str,
    stock_name: str = "",
    top_n: int = TOP_PEER_LIMIT,
) -> dict[str, Any]:
    started = time.perf_counter()
    normalized_id = str(stock_id or "").strip()
    normalized_name = str(stock_name or "").strip() or _stock_name(normalized_id)
    now = time.time()
    with _CACHE_LOCK:
        cached = _RESULT_CACHE.get(normalized_id)
        if cached and now - cached[0] <= CACHE_TTL_SECONDS:
            result = dict(cached[1])
            result["cached"] = True
            result["seconds"] = round(time.perf_counter() - started, 3)
            return result

    concepts = _concepts_for(normalized_id)
    if not concepts:
        return {
            "ok": True,
            "available": False,
            "message": "這檔股票尚未收錄概念族群",
            "stock_id": normalized_id,
            "stock_name": normalized_name,
            "concepts": [],
            "comparisons": [],
            "catalog_source": CATALOG_SOURCE_NAME,
            "catalog_source_url": CATALOG_SOURCE_URL,
            "catalog_updated_at": CATALOG_UPDATED_AT,
            "catalog_maintenance": CATALOG_MAINTENANCE_MODE,
            "catalog_group_count": CATALOG_GROUP_COUNT,
            "catalog_stock_count": CATALOG_STOCK_COUNT,
            "catalog_completeness": CATALOG_COMPLETENESS,
            "version": STOCK_CONCEPT_PEER_VERSION,
            "seconds": round(time.perf_counter() - started, 3),
        }

    candidate_concepts: dict[str, list[str]] = {}
    for concept in concepts:
        for candidate_id in CONCEPT_GROUPS[concept]:
            if candidate_id == normalized_id:
                continue
            if not _is_supported_comparison_stock(candidate_id):
                continue
            candidate_concepts.setdefault(candidate_id, []).append(concept)
    anchor_peers = set(CONCEPT_ANCHOR_PEERS.get(normalized_id, []))

    # 先保留已確認配對，再優先選擇成員較少、定義較精確的子族群。
    # 例如台光電會先比較 CCL，而不是先被大型低軌衛星清單占滿。
    def candidate_priority(candidate_id: str) -> tuple[int, float, int]:
        shared = candidate_concepts.get(candidate_id, [])
        specificity = max(
            (
                1.0 / max(len(CONCEPT_GROUPS.get(concept, {})), 1)
                for concept in shared
            ),
            default=0.0,
        )
        return (
            1 if candidate_id in anchor_peers else 0,
            specificity,
            len(shared),
        )

    candidate_ids = sorted(
        candidate_concepts,
        key=candidate_priority,
        reverse=True,
    )[:MAX_CANDIDATES]
    frames: dict[str, pd.DataFrame] = {}
    errors: dict[str, str] = {}
    all_ids = [normalized_id, *candidate_ids]
    with ThreadPoolExecutor(max_workers=min(5, len(all_ids))) as executor:
        futures = {
            executor.submit(_daily_frame, current_id): current_id
            for current_id in all_ids
        }
        for future in as_completed(futures):
            current_id = futures[future]
            try:
                frames[current_id] = future.result()
            except Exception as exc:
                frames[current_id] = pd.DataFrame()
                errors[current_id] = repr(exc)

    target = frames.get(normalized_id, pd.DataFrame())
    if target.empty:
        return {
            "ok": False,
            "available": False,
            "message": "目前抓不到主要股票的日線資料",
            "stock_id": normalized_id,
            "stock_name": normalized_name,
            "concepts": concepts,
            "comparisons": [],
            "errors": errors,
            "catalog_source": CATALOG_SOURCE_NAME,
            "catalog_source_url": CATALOG_SOURCE_URL,
            "catalog_updated_at": CATALOG_UPDATED_AT,
            "catalog_maintenance": CATALOG_MAINTENANCE_MODE,
            "catalog_group_count": CATALOG_GROUP_COUNT,
            "catalog_stock_count": CATALOG_STOCK_COUNT,
            "catalog_completeness": CATALOG_COMPLETENESS,
            "version": STOCK_CONCEPT_PEER_VERSION,
            "seconds": round(time.perf_counter() - started, 3),
        }

    comparisons: list[dict[str, Any]] = []
    for candidate_id in candidate_ids:
        peer = frames.get(candidate_id, pd.DataFrame())
        if peer.empty:
            continue
        comparison = _calculate_pair(
            target_id=normalized_id,
            target_name=normalized_name,
            target=target,
            peer_id=candidate_id,
            peer_name=_stock_name(candidate_id),
            peer=peer,
            shared_concepts=candidate_concepts[candidate_id],
        )
        if comparison:
            comparisons.append(comparison)

    comparisons.sort(
        key=lambda item: (
            1 if str(item.get("peer_id") or "") in anchor_peers else 0,
            _safe_float(item.get("similarity_pct")),
        ),
        reverse=True,
    )
    comparisons = comparisons[:max(1, min(int(top_n), TOP_PEER_LIMIT))]
    for comparison in comparisons:
        comparison["chart_url"] = _publish_ratio_chart(comparison)
        comparison.pop("chart_rows", None)

    result = {
        "ok": True,
        "available": bool(comparisons),
        "message": "ok" if comparisons else "同族群可比較日線資料不足",
        "stock_id": normalized_id,
        "stock_name": normalized_name,
        "concepts": concepts,
        "comparisons": comparisons,
        "catalog_source": CATALOG_SOURCE_NAME,
        "catalog_source_url": CATALOG_SOURCE_URL,
        "catalog_updated_at": CATALOG_UPDATED_AT,
        "catalog_maintenance": CATALOG_MAINTENANCE_MODE,
        "catalog_group_count": CATALOG_GROUP_COUNT,
        "catalog_stock_count": CATALOG_STOCK_COUNT,
        "catalog_completeness": CATALOG_COMPLETENESS,
        "methodology": {
            "window_days": SIMILARITY_WINDOW_DAYS,
            "selection": (
                "先取同一 CMoney 概念族群，"
                "人工確認同業優先，其餘依相似度排序"
            ),
            "similarity": (
                "日報酬相關50%＋同向率25%＋"
                "波動接近15%＋量能連動10%"
            ),
            "ratio": "主要股票收盤價 ÷ 比較股票收盤價",
            "classification": (
                "CMoney 概念分類為主、人工覆寫為輔；"
                "同一股票可有多個概念標籤"
            ),
        },
        "note": (
            "概念分類與歷史連動僅供觀察；"
            "相似度不代表未來仍同步，也不是交易建議。"
        ),
        "errors": errors,
        "cached": False,
        "version": STOCK_CONCEPT_PEER_VERSION,
        "seconds": round(time.perf_counter() - started, 3),
    }
    if comparisons:
        with _CACHE_LOCK:
            _RESULT_CACHE[normalized_id] = (time.time(), result)
    _debug(
        "built",
        "| stock =", normalized_id,
        "| concepts =", concepts,
        "| comparisons =", len(comparisons),
        "| best =",
        comparisons[0].get("peer_id") if comparisons else "",
        "| sec =", result["seconds"],
    )
    return result


def build_stock_group_comparison(
    stock_id: str,
    stock_name: str = "",
    top_n: int = TOP_PEER_LIMIT,
) -> dict[str, Any]:
    """新版公開入口；舊函式名稱保留給已部署程式相容使用。"""
    return build_stock_concept_peer_comparison(
        stock_id=stock_id,
        stock_name=stock_name,
        top_n=top_n,
    )
