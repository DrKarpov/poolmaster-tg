"""Stateless private coin menu; each toss stays inside the client widget."""
from functools import lru_cache
from pathlib import Path
import zlib

from keyboards import private_user_keyboard
from localization import normalize_language
from widget_transport import WidgetReceipt, WidgetTransport, wire_text


@lru_cache(maxsize=2)
def load_coin_widget_html(language: str) -> str:
    content=(Path(__file__).resolve().parent/'assets/widgets/coin/widget.html').read_text()
    if normalize_language(language)=='en':
        for original,translated in (
            ('Подбросить монетку TST','Toss the TST coin'),
            ('Подбросить монетку','Toss the coin'),
            ('Гладкая сторона.','Blank side.'),
            ('Логотип TST.','TST logo.'),
        ):
            content=content.replace(original,translated)
    from widget_compat import with_numeric_compat
    content = with_numeric_compat(content)
    wire_text(content,32768)
    if len(zlib.compress(content.encode(),9))>4096:
        raise ValueError('coin widget exceeds the compressed size budget')
    return content


async def send_coin_menu(transport: WidgetTransport, chat_id: int, language: str) -> WidgetReceipt:
    return await transport.send_private(chat_id,load_coin_widget_html(language),'{}','{}','',
        private_user_keyboard('home',language=language).to_dict(),allow_queries=False,width=320,height=320)
