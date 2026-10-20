from __future__ import annotations

from telegram import InlineQueryResultArticle, InputTextMessageContent
from telegram.constants import ParseMode

from farming import FARMING_POOLS
from keyboards import inline_price_refresh_keyboard, inline_result_keyboard
from snapshot import DataSnapshot
from user_ui import render_farming_text, render_inline_price_text


INLINE_CACHE_SECONDS = 5


def build_inline_results(
    snapshot: DataSnapshot | None,
    *,
    invalid_pool_ids: set[str] | frozenset[str] = frozenset(),
) -> list[InlineQueryResultArticle]:
    if snapshot is None:
        farming_text = (
            "<b>Фарминг JETTON</b>\n\nДанные DeDust и STON.fi временно недоступны. "
            "Попробуйте позже."
        )
    else:
        metrics = [snapshot.farming[pool.address] for pool in FARMING_POOLS if pool.address in snapshot.farming]
        farming_text = render_farming_text(
            metrics,
            invalid_pool_ids=invalid_pool_ids,
            anomalies=snapshot.anomalies,
        )
        if len(metrics) < len(FARMING_POOLS):
            if not metrics:
                farming_text += "\n\nДанные фарминга временно недоступны."
            else:
                farming_text += "\n\n⚠️ Данные части пулов временно недоступны."

    farming_keyboard = inline_result_keyboard()
    price_text = build_inline_price_text(snapshot)
    price_result = InlineQueryResultArticle(
        id="jetton-price",
        title="Цена JETTON",
        description="MEXC Spot · цена и статистика за 24 ч",
        input_message_content=InputTextMessageContent(
            message_text=price_text,
            parse_mode=ParseMode.HTML,
        ),
        reply_markup=inline_price_refresh_keyboard(),
    )

    return [
        price_result,
        InlineQueryResultArticle(
            id="jetton-farming",
            title="Фарминг JETTON",
            description="Пулы DeDust и STON.fi",
            input_message_content=InputTextMessageContent(
                message_text=farming_text,
                parse_mode=ParseMode.HTML,
            ),
            reply_markup=farming_keyboard,
        ),
]


def build_inline_price_text(snapshot: DataSnapshot | None) -> str:
    if snapshot is None:
        price_text = (
            "<b>Цена JETTON</b>\n\nДанные цены временно недоступны.\n"
            "Источник: MEXC Spot"
        )
    else:
        price_text = render_inline_price_text(snapshot.price)
    return price_text
