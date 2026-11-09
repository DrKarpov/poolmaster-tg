import asyncio
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import json

import pytest

from farming_widget import encode_farming_view, decode_farming_view
from farming_widget_data import FarmingWidgetView, FarmingWidgetPoint
from test_farming_widget_data import NOW


def test_payload_contains_all_weeks_and_four_series():
    points = tuple(FarmingWidgetPoint(date(2026,5,25)+timedelta(weeks=i),date(2026,5,31)+timedelta(weeks=i),
                   (Decimal('101.89'),None,Decimal('32.75'),Decimal('0'))) for i in range(70))
    view = FarmingWidgetView('7D',points,Decimal('25'),Decimal('1000'),NOW,'ok')
    parts = encode_farming_view(view,4)
    assert len(parts.public_data.encode()) <= 900 and len(parts.chat_data.encode()) <= 900
    assert decode_farming_view(parts) == view
    assert json.loads(parts.public_data)['r'] == 4


def test_payload_rounds_for_display_without_mutating_source():
    view = FarmingWidgetView('7D',(FarmingWidgetPoint(date(2026,5,25),date(2026,5,31),(Decimal('101.896'),)*4),),Decimal('25.125'),Decimal('1000.789'),NOW,'ok')
    decoded = decode_farming_view(encode_farming_view(view,1))
    assert decoded.points[0].values[0] == Decimal('101.90')
    assert view.points[0].values[0] == Decimal('101.896')


@pytest.mark.parametrize('period',['1D','3D','other'])
def test_reserved_periods_are_not_supported(period):
    view = FarmingWidgetView(period,(),None,None,NOW,'unavailable')
    with pytest.raises(ValueError): encode_farming_view(view,1)


def test_oversize_history_is_never_silently_truncated():
    points = tuple(FarmingWidgetPoint(date(2020,1,6)+timedelta(weeks=i),date(2020,1,12)+timedelta(weeks=i),(Decimal('12345.67'),)*4) for i in range(300))
    with pytest.raises(ValueError):
        encode_farming_view(FarmingWidgetView('7D',points,None,None,NOW,'ok'),1)
