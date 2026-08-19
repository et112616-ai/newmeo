# -*- coding: utf-8 -*-

"""
test_disposition.py

用途：
    測試台股處置股票資料

資料來源：
    1. TWSE 證交所
    2. TPEX 櫃買中心（若公開 API 可直接取得）

篩選：
    只保留「4 碼以內的純數字代號」
    → 排除權證及其他衍生商品

輸出：
    市場
    股票代號
    股票名稱
    處置期間
    撮合分鐘數
    官方處置內容
"""

import re
from datetime import date
import requests


# ============================================================
# API
# ============================================================

TWSE_URL = (
    "https://openapi.twse.com.tw/"
    "v1/announcement/punish"
)

# TPEX OpenAPI
TPEX_URL = (
    "https://www.tpex.org.tw/openapi/v1/"
    "tpex_disposal_information"
)

TIMEOUT = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/151.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


# ============================================================
# 基本工具
# ============================================================

def clean_text(value):
    if value is None:
        return ""

    return str(value).strip()


def is_stock_code(code):
    """
    只接受：
        純數字
        4 碼以下

    例如：
        2330   True
        0050   True
        3037   True
        00981A False
        716819 False
    """

    code = clean_text(code)

    return (
        bool(re.fullmatch(r"\d+", code))
        and len(code) <= 4
    )


# ============================================================
# 日期
# ============================================================

def parse_date(value):
    """
    支援：

    2026/08/18
    2026-08-18
    115/08/18
    1150818
    """

    value = clean_text(value)

    if not value:
        return None

    # 西元
    m = re.fullmatch(
        r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})",
        value,
    )

    if m:
        try:
            return date(
                int(m.group(1)),
                int(m.group(2)),
                int(m.group(3)),
            )
        except ValueError:
            return None

    # 民國
    m = re.fullmatch(
        r"(\d{3})[/-](\d{1,2})[/-](\d{1,2})",
        value,
    )

    if m:
        try:
            return date(
                int(m.group(1)) + 1911,
                int(m.group(2)),
                int(m.group(3)),
            )
        except ValueError:
            return None

    # 民國 7 碼
    m = re.fullmatch(
        r"(\d{3})(\d{2})(\d{2})",
        value,
    )

    if m:
        try:
            return date(
                int(m.group(1)) + 1911,
                int(m.group(2)),
                int(m.group(3)),
            )
        except ValueError:
            return None

    return None


def parse_period(value):
    """
    解析：

    115/08/18~115/08/29
    115/08/18～115/08/29
    2026/08/18~2026/08/29
    """

    value = clean_text(value)

    if not value:
        return None, None

    parts = re.split(r"[~～]", value)

    if len(parts) != 2:
        return None, None

    start = parse_date(parts[0])
    end = parse_date(parts[1])

    return start, end


# ============================================================
# 撮合分鐘數
# ============================================================

CHINESE_NUMBER = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
    "十三": 13,
    "十四": 14,
    "十五": 15,
    "十六": 16,
    "十七": 17,
    "十八": 18,
    "十九": 19,
    "二十": 20,
    "二十一": 21,
    "二十二": 22,
    "二十三": 23,
    "二十四": 24,
    "二十五": 25,
    "四十五": 45,
    "六十": 60,
}


def detect_matching_minutes(text):
    """
    從官方處置內容抓：

    約每5分鐘撮合一次
    約每20分鐘撮合一次
    約每10分鐘撮合一次
    約每25分鐘撮合一次
    約每45分鐘撮合一次
    約每60分鐘撮合一次

    也接受：
    約每五分鐘撮合一次
    """

    text = clean_text(text)

    if not text:
        return None

    # --------------------------
    # 阿拉伯數字
    # --------------------------

    match = re.search(
        r"約每\s*(\d+)\s*分鐘\s*撮合",
        text,
    )

    if match:
        return int(match.group(1))

    # --------------------------
    # 中文數字
    # --------------------------

    for chinese, number in sorted(
        CHINESE_NUMBER.items(),
        key=lambda x: len(x[0]),
        reverse=True,
    ):
        pattern = (
            rf"約每\s*{re.escape(chinese)}"
            r"\s*分鐘\s*撮合"
        )

        if re.search(pattern, text):
            return number

    return None


# ============================================================
# TWSE
# ============================================================

def fetch_twse():

    print()
    print("=" * 70)
    print("TWSE 證交所")
    print("=" * 70)

    try:

        response = requests.get(
            TWSE_URL,
            headers=HEADERS,
            timeout=TIMEOUT,
        )

        print("HTTP:", response.status_code)

        response.raise_for_status()

        data = response.json()

        if not isinstance(data, list):
            print("❌ TWSE 回傳不是 list")
            print(data)
            return []

        print("官方原始資料：", len(data), "筆")

        result = []

        for item in data:

            code = clean_text(
                item.get("Code")
            )

            # ------------------------------------------------
            # 只保留 4 碼純數字
            # ------------------------------------------------

            if not is_stock_code(code):
                continue

            name = clean_text(
                item.get("Name")
            )

            period = clean_text(
                item.get("DispositionPeriod")
            )

            measures = clean_text(
                item.get("DispositionMeasures")
            )

            detail = clean_text(
                item.get("Detail")
            )

            start_date, end_date = parse_period(
                period
            )

            minutes = detect_matching_minutes(
                detail
            )

            if minutes is None:
                minutes = detect_matching_minutes(
                    measures
                )

            result.append({
                "market": "上市",
                "code": code,
                "name": name,
                "period": period,
                "start_date": start_date,
                "end_date": end_date,
                "minutes": minutes,
                "detail": detail,
                "measures": measures,
            })

        print(
            "過濾後股票：",
            len(result),
            "筆",
        )

        return result

    except Exception as e:

        print()
        print("❌ TWSE API 失敗")
        print(type(e).__name__)
        print(str(e))

        return []


# ============================================================
# TPEX
# ============================================================

def fetch_tpex():

    print()
    print("=" * 70)
    print("TPEX 櫃買中心")
    print("=" * 70)

    try:

        response = requests.get(
            TPEX_URL,
            headers=HEADERS,
            timeout=TIMEOUT,
        )

        print("HTTP:", response.status_code)

        response.raise_for_status()

        data = response.json()

        if not isinstance(data, list):
            print("❌ TPEX 回傳不是 list")
            print(data)
            return []

        print(
            "官方原始資料：",
            len(data),
            "筆",
        )

        result = []

        for item in data:

            # ------------------------------------------------
            # 嘗試多種可能欄位名稱
            # ------------------------------------------------

            code = ""

            for key in (
                "SecuritiesCompanyCode",
                "Code",
                "SecurityCode",
                "證券代號",
                "代號",
            ):

                if item.get(key):
                    code = clean_text(
                        item.get(key)
                    )
                    break

            # ------------------------------------------------
            # 只保留 4 碼純數字
            # ------------------------------------------------

            if not is_stock_code(code):
                continue

            name = ""

            for key in (
                "CompanyName",
                "Name",
                "SecurityName",
                "證券名稱",
                "名稱",
            ):

                if item.get(key):
                    name = clean_text(
                        item.get(key)
                    )
                    break

            period = ""

            for key in (
                "DispositionPeriod",
                "Period",
                "處置起訖時間",
            ):

                if item.get(key):
                    period = clean_text(
                        item.get(key)
                    )
                    break

            # ------------------------------------------------
            # 將所有欄位組合成文字
            # ------------------------------------------------

            detail_parts = []

            for key, value in item.items():

                if value is None:
                    continue

                text = clean_text(value)

                if text:
                    detail_parts.append(text)

            detail = " ".join(
                detail_parts
            )

            start_date, end_date = parse_period(
                period
            )

            minutes = detect_matching_minutes(
                detail
            )

            result.append({
                "market": "上櫃",
                "code": code,
                "name": name,
                "period": period,
                "start_date": start_date,
                "end_date": end_date,
                "minutes": minutes,
                "detail": detail,
            })

        print(
            "過濾後股票：",
            len(result),
            "筆",
        )

        return result

    except Exception as e:

        print()
        print("⚠️ TPEX API 無法直接取得")
        print(type(e).__name__)
        print(str(e))

        return []


# ============================================================
# 目前處置
# ============================================================

def filter_active(records):

    today = date.today()

    result = []

    for row in records:

        start = row.get("start_date")
        end = row.get("end_date")

        if not start or not end:
            continue

        if start <= today <= end:
            result.append(row)

    return result


# ============================================================
# 顯示
# ============================================================

def print_group(records):

    if not records:

        print("（無）")

        return

    for row in records:

        minutes = row.get("minutes")

        if minutes:
            frequency = f"約 {minutes} 分鐘"
        else:
            frequency = "未判斷"

        print(
            f"{row['market']} | "
            f"{row['code']} | "
            f"{row['name']} | "
            f"{row['period']} | "
            f"{frequency}"
        )


# ============================================================
# 主程式
# ============================================================

def main():

    today = date.today()

    print()
    print("=" * 70)
    print("台股處置股票測試")
    print("=" * 70)

    print(
        "測試日期：",
        today.strftime("%Y-%m-%d")
    )

    print(
        "股票篩選：4碼以下純數字"
    )

    # --------------------------------------------------------
    # 抓資料
    # --------------------------------------------------------

    twse = fetch_twse()

    tpex = fetch_tpex()

    all_records = twse + tpex

    # --------------------------------------------------------
    # 目前處置
    # --------------------------------------------------------

    active = filter_active(
        all_records
    )

    # --------------------------------------------------------
    # 排序
    # --------------------------------------------------------

    active.sort(
        key=lambda x: (
            x.get("minutes")
            if x.get("minutes") is not None
            else 999,
            x.get("code", ""),
        )
    )

    # --------------------------------------------------------
    # 分組
    # --------------------------------------------------------

    groups = {}

    for row in active:

        minutes = row.get("minutes")

        if minutes is None:
            key = "未判斷"
        else:
            key = f"{minutes}分鐘"

        groups.setdefault(
            key,
            []
        ).append(row)

    # --------------------------------------------------------
    # 結果
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("目前仍在處置期間的股票")
    print("=" * 70)

    if not active:

        print("目前沒有符合條件的處置股票")

    else:

        for group_name, rows in groups.items():

            print()
            print(
                f"【{group_name}】"
            )

            print("-" * 70)

            print_group(rows)

    # --------------------------------------------------------
    # 總結
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("總結")
    print("=" * 70)

    print(
        "TWSE 股票：",
        len(twse)
    )

    print(
        "TPEX 股票：",
        len(tpex)
    )

    print(
        "目前處置：",
        len(active)
    )

    print()

    for group_name, rows in groups.items():

        print(
            f"{group_name}：",
            len(rows),
            "檔"
        )

    # --------------------------------------------------------
    # Debug：顯示未判斷資料
    # --------------------------------------------------------

    unknown = [
        row
        for row in active
        if row.get("minutes") is None
    ]

    if unknown:

        print()
        print("=" * 70)
        print("⚠️ 無法判斷撮合時間的股票")
        print("=" * 70)

        for row in unknown:

            print()
            print(
                row["market"],
                row["code"],
                row["name"]
            )

            print(
                "期間：",
                row["period"]
            )

            print(
                "官方內容："
            )

            print(
                row.get(
                    "detail",
                    ""
                )[:1000]
            )

    print()
    print("=" * 70)
    print("測試完成")
    print("=" * 70)


if __name__ == "__main__":
    main()
