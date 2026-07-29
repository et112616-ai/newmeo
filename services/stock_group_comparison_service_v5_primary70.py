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
    "2026-07-29-v5-PRIMARY-GROUPS-CMONEY-AUX-70PCT"
)
STOCK_CONCEPT_PEER_VERSION = STOCK_GROUP_COMPARISON_VERSION
SIMILARITY_WINDOW_DAYS = max(
    60,
    min(int(os.getenv("STOCK_PEER_WINDOW_DAYS", "240")), 260),
)
MIN_SIMILARITY_PCT = max(
    0.0,
    min(
        float(os.getenv("STOCK_PEER_MIN_SIMILARITY_PCT", "70")),
        100.0,
    ),
)
TOP_PEER_LIMIT = max(
    1,
    min(int(os.getenv("STOCK_PEER_TOP_LIMIT", "3")), 5),
)
MAX_CANDIDATES = max(
    3,
    min(int(os.getenv("STOCK_PEER_MAX_CANDIDATES", "12")), 20),
)
CACHE_TTL_SECONDS = max(
    60,
    int(os.getenv("STOCK_PEER_CACHE_TTL_SECONDS", "900")),
)

SERVICE_DIR = Path(__file__).resolve().parent
PRIMARY_CATALOG_PATH = Path(
    os.getenv(
        "STOCK_PRIMARY_GROUP_CATALOG_PATH",
        str(SERVICE_DIR / "stock_primary_group_catalog_v1.json"),
    )
)
AUXILIARY_CATALOG_PATH = Path(
    os.getenv(
        "STOCK_CONCEPT_CATALOG_PATH",
        str(SERVICE_DIR / "cmoney_concept_catalog_v2.json"),
    )
)
OVERRIDE_PATH = Path(
    os.getenv(
        "STOCK_CONCEPT_OVERRIDE_PATH",
        str(SERVICE_DIR / "stock_group_manual_overrides_v2.json"),
    )
)

PRIMARY_SOURCE_NAME = "人工整理43類主分類"
AUXILIARY_SOURCE_NAME = "理財網/CMoney概念分類"
AUXILIARY_SOURCE_URL = "https://www.cmoney.tw/forum/concept"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except Exception as exc:
        print(
            "DEBUG stock_group_comparison_v5 | load failed",
            "| path =", str(path),
            "| error =", repr(exc),
            flush=True,
        )
        return {}


def _normalize_groups(
    payload: dict[str, Any],
) -> dict[str, dict[str, str]]:
    normalized: dict[str, dict[str, str]] = {}
    for group_name, group_payload in (payload.get("groups") or {}).items():
        if not isinstance(group_payload, dict):
            continue
        members = (
            group_payload.get("members")
            if "members" in group_payload
            else group_payload
        )
        if not isinstance(members, dict):
            continue
        clean_members = {
            str(stock_id).strip(): str(stock_name or stock_id).strip()
            for stock_id, stock_name in members.items()
            if str(stock_id).strip()
        }
        if clean_members:
            normalized[str(group_name).strip()] = clean_members
    return normalized


def _apply_group_overrides(
    groups: dict[str, dict[str, str]],
    changes: dict[str, Any],
) -> None:
    for group_name, group_changes in changes.items():
        if not isinstance(group_changes, dict):
            continue
        members = groups.setdefault(str(group_name).strip(), {})
        for stock_id in group_changes.get("remove") or []:
            members.pop(str(stock_id).strip(), None)
        for stock_id, stock_name in (group_changes.get("add") or {}).items():
            clean_id = str(stock_id).strip()
            if clean_id:
                members[clean_id] = str(stock_name or clean_id).strip()
        if not members:
            groups.pop(str(group_name).strip(), None)


_PRIMARY_PAYLOAD = _load_json(PRIMARY_CATALOG_PATH)
_AUXILIARY_PAYLOAD = _load_json(AUXILIARY_CATALOG_PATH)
_OVERRIDES = _load_json(OVERRIDE_PATH)

PRIMARY_GROUPS = _normalize_groups(_PRIMARY_PAYLOAD)
AUXILIARY_GROUPS = _normalize_groups(_AUXILIARY_PAYLOAD)
_apply_group_overrides(
    PRIMARY_GROUPS,
    _OVERRIDES.get("primary_groups") or {},
)
_apply_group_overrides(
    AUXILIARY_GROUPS,
    _OVERRIDES.get("auxiliary_groups") or {},
)

ANCHOR_PEERS: dict[str, list[str]] = {}
for _stock_id, _peer_ids in (_OVERRIDES.get("anchor_peers") or {}).items():
    if isinstance(_peer_ids, list):
        ANCHOR_PEERS[str(_stock_id).strip()] = [
            str(peer_id).strip()
            for peer_id in _peer_ids
            if str(peer_id).strip()
        ]

CATALOG_UPDATED_AT = str(
    _OVERRIDES.get("updated_at")
    or _PRIMARY_PAYLOAD.get("updated_at")
    or "2026-07-29"
)
PRIMARY_GROUP_COUNT = len(PRIMARY_GROUPS)
PRIMARY_STOCK_COUNT = len(
    {
        stock_id
        for members in PRIMARY_GROUPS.values()
        for stock_id in members
    }
)
AUXILIARY_GROUP_COUNT = len(AUXILIARY_GROUPS)

_CACHE_LOCK = threading.Lock()
_RESULT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except Exception:
        return default


def _concepts_for(
    stock_id: str,
    groups: dict[str, dict[str, str]],
) -> list[str]:
    return [
        group_name
        for group_name, members in groups.items()
        if stock_id in members
    ]


def _stock_name(stock_id: str) -> str:
    for groups in (PRIMARY_GROUPS, AUXILIARY_GROUPS):
        for members in groups.values():
            if stock_id in members:
                name = str(members[stock_id] or "").strip()
                if name and name != stock_id:
                    return name
    try:
        meta = normalize_stock_input(stock_id)
        name = str(
            getattr(meta, "stock_name", "")
            or getattr(meta, "name", "")
            or ""
        ).strip()
        if name:
            return name
    except Exception:
        pass
    return stock_id


def _is_supported_stock(stock_id: str) -> bool:
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
        "chart_rows": [
            {
                "date": pd.Timestamp(index).strftime("%Y-%m-%d"),
                "ratio": round(_safe_float(value), 8),
            }
            for index, value in ratio.items()
        ],
        "chart_url": "",
    }


def _add_candidates(
    candidate_meta: dict[str, dict[str, Any]],
    target_id: str,
    concepts: list[str],
    groups: dict[str, dict[str, str]],
    source_scope: str,
) -> None:
    for concept in concepts:
        members = groups.get(concept, {})
        for candidate_id in members:
            if candidate_id == target_id:
                continue
            if not _is_supported_stock(candidate_id):
                continue
            meta = candidate_meta.setdefault(
                candidate_id,
                {
                    "primary_concepts": [],
                    "auxiliary_concepts": [],
                    "source_scope": source_scope,
                },
            )
            key = (
                "primary_concepts"
                if source_scope == "primary"
                else "auxiliary_concepts"
            )
            if concept not in meta[key]:
                meta[key].append(concept)
            if source_scope == "primary":
                meta["source_scope"] = "primary"


def _candidate_priority(
    candidate_id: str,
    meta: dict[str, Any],
    anchor_peers: set[str],
) -> tuple[int, int, float, int]:
    is_primary = meta.get("source_scope") == "primary"
    concepts = (
        meta.get("primary_concepts")
        if is_primary
        else meta.get("auxiliary_concepts")
    ) or []
    groups = PRIMARY_GROUPS if is_primary else AUXILIARY_GROUPS
    specificity = max(
        (
            1.0 / max(len(groups.get(concept, {})), 1)
            for concept in concepts
        ),
        default=0.0,
    )
    return (
        1 if is_primary else 0,
        1 if candidate_id in anchor_peers else 0,
        specificity,
        len(concepts),
    )


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
        frame["ratio"] = pd.to_numeric(frame["ratio"], errors="coerce")
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
            label=f"{SIMILARITY_WINDOW_DAYS}日均值",
        )
        ax.scatter(
            frame["date"].iloc[-1],
            current_value,
            color="#7C3AED",
            s=28,
            zorder=5,
        )
        ax.grid(True, color="#D1D5DB", linewidth=0.6, alpha=0.55)
        ax.xaxis.set_major_locator(
            mdates.AutoDateLocator(minticks=4, maxticks=6)
        )
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
        ax.tick_params(axis="both", labelsize=9, colors="#4B5563")
        ax.set_ylabel("股價比值", fontsize=9, color="#4B5563")
        for spine in ax.spines.values():
            spine.set_color("#D1D5DB")
        ax.legend(loc="upper left", fontsize=8, frameon=False, ncol=3)
        fig.tight_layout(pad=0.8)
        name = (
            f"{comparison.get('target_id')}_"
            f"{comparison.get('peer_id')}_peer_ratio_v5"
        )
        return str(publish_figure(fig, name) or "")
    except Exception as exc:
        print(
            "DEBUG stock_group_comparison_v5 | chart failed",
            "| error =", repr(exc),
            flush=True,
        )
        return ""


def _base_metadata() -> dict[str, Any]:
    return {
        "catalog_source": PRIMARY_SOURCE_NAME,
        "catalog_source_url": AUXILIARY_SOURCE_URL,
        "catalog_updated_at": CATALOG_UPDATED_AT,
        "catalog_maintenance": "人工主分類＋理財網輔助＋人工覆寫",
        "catalog_group_count": PRIMARY_GROUP_COUNT,
        "catalog_stock_count": PRIMARY_STOCK_COUNT,
        "primary_group_count": PRIMARY_GROUP_COUNT,
        "primary_stock_count": PRIMARY_STOCK_COUNT,
        "auxiliary_source": AUXILIARY_SOURCE_NAME,
        "auxiliary_group_count": AUXILIARY_GROUP_COUNT,
        "minimum_similarity_pct": MIN_SIMILARITY_PCT,
    }


def build_stock_concept_peer_comparison(
    stock_id: str,
    stock_name: str = "",
    top_n: int = TOP_PEER_LIMIT,
) -> dict[str, Any]:
    started = time.perf_counter()
    normalized_id = str(stock_id or "").strip()
    normalized_name = str(stock_name or "").strip() or _stock_name(normalized_id)

    with _CACHE_LOCK:
        cached = _RESULT_CACHE.get(normalized_id)
        if cached and time.time() - cached[0] <= CACHE_TTL_SECONDS:
            result = dict(cached[1])
            result["cached"] = True
            result["seconds"] = round(time.perf_counter() - started, 3)
            return result

    primary_concepts = _concepts_for(normalized_id, PRIMARY_GROUPS)
    auxiliary_concepts = _concepts_for(normalized_id, AUXILIARY_GROUPS)
    if not primary_concepts and not auxiliary_concepts:
        return {
            "ok": True,
            "available": False,
            "message": "這檔股票尚未收錄主分類或理財網輔助分類",
            "stock_id": normalized_id,
            "stock_name": normalized_name,
            "concepts": [],
            "primary_concepts": [],
            "auxiliary_concepts": [],
            "comparisons": [],
            **_base_metadata(),
            "version": STOCK_CONCEPT_PEER_VERSION,
            "seconds": round(time.perf_counter() - started, 3),
        }

    candidate_meta: dict[str, dict[str, Any]] = {}
    _add_candidates(
        candidate_meta,
        normalized_id,
        primary_concepts,
        PRIMARY_GROUPS,
        "primary",
    )

    # 主分類候選不足時才以理財網概念補足，不讓輔助分類覆蓋主分類。
    if len(candidate_meta) < MAX_CANDIDATES:
        _add_candidates(
            candidate_meta,
            normalized_id,
            auxiliary_concepts,
            AUXILIARY_GROUPS,
            "auxiliary",
        )

    anchor_peers = set(ANCHOR_PEERS.get(normalized_id, []))
    candidate_ids = sorted(
        candidate_meta,
        key=lambda candidate_id: _candidate_priority(
            candidate_id,
            candidate_meta[candidate_id],
            anchor_peers,
        ),
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
            "concepts": primary_concepts or auxiliary_concepts,
            "primary_concepts": primary_concepts,
            "auxiliary_concepts": auxiliary_concepts,
            "comparisons": [],
            "errors": errors,
            **_base_metadata(),
            "version": STOCK_CONCEPT_PEER_VERSION,
            "seconds": round(time.perf_counter() - started, 3),
        }

    comparisons: list[dict[str, Any]] = []
    for candidate_id in candidate_ids:
        peer = frames.get(candidate_id, pd.DataFrame())
        if peer.empty:
            continue
        meta = candidate_meta[candidate_id]
        shared_concepts = (
            meta.get("primary_concepts")
            if meta.get("source_scope") == "primary"
            else meta.get("auxiliary_concepts")
        ) or []
        comparison = _calculate_pair(
            target_id=normalized_id,
            target_name=normalized_name,
            target=target,
            peer_id=candidate_id,
            peer_name=_stock_name(candidate_id),
            peer=peer,
            shared_concepts=shared_concepts,
        )
        if not comparison:
            continue
        comparison["classification_source"] = (
            PRIMARY_SOURCE_NAME
            if meta.get("source_scope") == "primary"
            else AUXILIARY_SOURCE_NAME
        )
        comparison["classification_scope"] = meta.get("source_scope")
        comparison["primary_shared_groups"] = meta.get(
            "primary_concepts",
            [],
        )
        comparison["auxiliary_shared_groups"] = meta.get(
            "auxiliary_concepts",
            [],
        )
        # 人工 anchor 只能提早計算，仍必須通過相同的70%門檻。
        if _safe_float(comparison.get("similarity_pct")) >= MIN_SIMILARITY_PCT:
            comparisons.append(comparison)

    display_limit = max(1, min(int(top_n), TOP_PEER_LIMIT))
    primary_matches = sorted(
        (
            item
            for item in comparisons
            if item.get("classification_scope") == "primary"
        ),
        key=lambda item: _safe_float(item.get("similarity_pct")),
        reverse=True,
    )
    auxiliary_matches = sorted(
        (
            item
            for item in comparisons
            if item.get("classification_scope") != "primary"
        ),
        key=lambda item: _safe_float(item.get("similarity_pct")),
        reverse=True,
    )
    # 主分類達門檻者一定先顯示；不足顯示張數時才用輔助分類補位。
    comparisons = primary_matches[:display_limit]
    if len(comparisons) < display_limit:
        comparisons.extend(
            auxiliary_matches[: display_limit - len(comparisons)]
        )
    for comparison in comparisons:
        comparison["chart_url"] = _publish_ratio_chart(comparison)
        comparison.pop("chart_rows", None)

    result = {
        "ok": True,
        "available": bool(comparisons),
        "message": (
            "ok"
            if comparisons
            else f"目前沒有相似度達{MIN_SIMILARITY_PCT:g}%的同族群標的"
        ),
        "stock_id": normalized_id,
        "stock_name": normalized_name,
        "concepts": primary_concepts or auxiliary_concepts,
        "primary_concepts": primary_concepts,
        "auxiliary_concepts": auxiliary_concepts,
        "comparisons": comparisons,
        **_base_metadata(),
        "methodology": {
            "window_days": SIMILARITY_WINDOW_DAYS,
            "minimum_similarity_pct": MIN_SIMILARITY_PCT,
            "selection": (
                "先取人工主分類同族群；主分類候選不足時，"
                "才由理財網/CMoney概念分類補充；最後僅顯示達門檻者"
            ),
            "similarity": (
                "日報酬相關50%＋同向率25%＋"
                "波動接近15%＋量能連動10%"
            ),
            "ratio": "主要股票收盤價 ÷ 比較股票收盤價",
            "classification": (
                "人工整理43類為主；理財網/CMoney僅為輔；"
                "人工增刪於覆寫檔維護"
            ),
        },
        "note": (
            "相似度以過去可取得的最多240個共同交易日估算；"
            "歷史連動不代表未來仍同步，也不是交易建議。"
        ),
        "errors": errors,
        "cached": False,
        "version": STOCK_CONCEPT_PEER_VERSION,
        "seconds": round(time.perf_counter() - started, 3),
    }
    if comparisons:
        with _CACHE_LOCK:
            _RESULT_CACHE[normalized_id] = (time.time(), result)
    print(
        "DEBUG stock_group_comparison_v5 | built",
        "| stock =", normalized_id,
        "| primary =", primary_concepts,
        "| auxiliary =", auxiliary_concepts,
        "| threshold =", MIN_SIMILARITY_PCT,
        "| comparisons =", len(comparisons),
        "| sec =", result["seconds"],
        flush=True,
    )
    return result


def build_stock_group_comparison(
    stock_id: str,
    stock_name: str = "",
    top_n: int = TOP_PEER_LIMIT,
) -> dict[str, Any]:
    return build_stock_concept_peer_comparison(
        stock_id=stock_id,
        stock_name=stock_name,
        top_n=top_n,
    )


__all__ = [
    "STOCK_GROUP_COMPARISON_VERSION",
    "STOCK_CONCEPT_PEER_VERSION",
    "SIMILARITY_WINDOW_DAYS",
    "MIN_SIMILARITY_PCT",
    "PRIMARY_GROUPS",
    "AUXILIARY_GROUPS",
    "build_stock_concept_peer_comparison",
    "build_stock_group_comparison",
]
