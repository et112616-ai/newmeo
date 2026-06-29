from __future__ import annotations

from flex.flex_builder import futures_notice, image_dashboard, text_message
from flex.table_builder import large_holder_table, margin_table
from services.chart_service import generate_chip_chart, generate_instant_chart, generate_kline_chart
from services.chip_service import get_institutional_chips, get_large_holder_table, get_margin_table
from services.stock_service import build_price_meta, get_history, get_stock_name, normalize_stock_input
from utils.formatter import normalize_time_frame
from utils.parser import BotRequest


def handle_request(req: BotRequest) -> dict:
    action = (req.action or "instant").strip()
    time_frame = normalize_time_frame(req.time_frame)

    meta = normalize_stock_input(req.stock)
    stock_name = get_stock_name(meta)

    if action == "futures":
        return futures_notice(meta.stock_id, stock_name)

    if action == "large_holder":
        rows = get_large_holder_table(meta.stock_id)
        return large_holder_table(meta.stock_id, stock_name, rows, time_frame)

    if action == "margin":
        rows = get_margin_table(meta.stock_id)
        return margin_table(meta.stock_id, stock_name, rows, time_frame)

    if action == "legal_person":
        df, normalized_tf = get_history(meta, "D")
        price = build_price_meta(df, normalized_tf)
        chips = get_institutional_chips(meta.stock_id)
        image_url = generate_chip_chart(meta.stock_id, stock_name, chips)
        return image_dashboard(
            stock_id=meta.stock_id,
            stock_name=stock_name,
            image_url=image_url,
            current_mode="legal_person",
            time_frame=time_frame,
            price_info=price.price_info,
            change_info=price.change_info,
            time_stamp=price.time_stamp,
            price_change=price.price_change,
            title="法人籌碼",
        )

    # 即時與 K 線都需要價格資料。
    df, normalized_tf = get_history(meta, "1m" if action == "instant" else time_frame)
    if df.empty:
        return text_message(f"❌ 找不到 {req.stock} 的 yfinance 資料。請確認代號是否正確，或改輸入股票代號，例如 2330。")

    price = build_price_meta(df, normalized_tf)

    if action == "k_line":
        image_url = generate_kline_chart(df, meta.stock_id, stock_name, normalized_tf)
        return image_dashboard(
            stock_id=meta.stock_id,
            stock_name=stock_name,
            image_url=image_url,
            current_mode="k_line",
            time_frame=normalized_tf,
            price_info=price.price_info,
            change_info=price.change_info,
            time_stamp=price.time_stamp,
            price_change=price.price_change,
            title="K線圖",
        )

    # 預設 instant
    image_url = generate_instant_chart(df, meta.stock_id, stock_name)
    return image_dashboard(
        stock_id=meta.stock_id,
        stock_name=stock_name,
        image_url=image_url,
        current_mode="instant",
        time_frame="1m" if normalized_tf == "1m" else time_frame,
        price_info=price.price_info,
        change_info=price.change_info,
        time_stamp=price.time_stamp,
        price_change=price.price_change,
        title="即時走勢",
    )
