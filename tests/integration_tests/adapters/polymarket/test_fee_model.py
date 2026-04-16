# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------
"""
Tests for the PolymarketFeeModel.

The model implements Polymarket's dynamic fee formula:
    fee = quantity * price * fee_rate * (1 - price)

Fees peak at 50c and decrease toward both extremes (0c and 100c).
"""

from decimal import Decimal
from unittest.mock import MagicMock

from nautilus_trader.adapters.polymarket.fee_model import PolymarketFeeModel
from nautilus_trader.model.currencies import USDC_POS
from nautilus_trader.model.enums import LiquiditySide
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity


def _create_mock_order(liquidity_side: LiquiditySide) -> MagicMock:
    """Create a mock order with specified liquidity side."""
    order = MagicMock()
    order.liquidity_side = liquidity_side
    return order


def _create_mock_instrument(taker_fee: Decimal) -> MagicMock:
    """Create a mock instrument with specified taker fee and real Currency."""
    instrument = MagicMock()
    instrument.taker_fee = taker_fee
    instrument.quote_currency = USDC_POS
    return instrument


class TestPolymarketFeeModel:
    """Tests for PolymarketFeeModel."""

    def test_maker_pays_zero_commission(self) -> None:
        """Makers should pay zero fees regardless of price."""
        model = PolymarketFeeModel()
        order = _create_mock_order(LiquiditySide.MAKER)
        instrument = _create_mock_instrument(Decimal("0.072"))

        commission = model.get_commission(
            order, Quantity.from_str("10"), Price.from_str("0.95"), instrument,
        )

        assert commission.as_decimal() == Decimal(0)

    def test_taker_pays_dynamic_fee_at_50c(self) -> None:
        """Fee should be maximum at 50c (p * (1-p) = 0.25)."""
        model = PolymarketFeeModel()
        order = _create_mock_order(LiquiditySide.TAKER)
        instrument = _create_mock_instrument(Decimal("0.072"))

        commission = model.get_commission(
            order, Quantity.from_str("10"), Price.from_str("0.50"), instrument,
        )

        # fee = 10 * 0.072 * 0.50 * 0.50 = 0.18
        assert commission.as_decimal() == Decimal("0.18")

    def test_taker_pays_reduced_fee_at_95c(self) -> None:
        """Fee should be much lower at 95c due to (1-p) = 0.05 factor."""
        model = PolymarketFeeModel()
        order = _create_mock_order(LiquiditySide.TAKER)
        instrument = _create_mock_instrument(Decimal("0.072"))

        commission = model.get_commission(
            order, Quantity.from_str("10"), Price.from_str("0.95"), instrument,
        )

        # fee = 10 * 0.072 * 0.95 * 0.05 = 0.0342
        assert commission.as_decimal() == Decimal("0.0342")

    def test_taker_pays_minimal_fee_at_99c(self) -> None:
        """Fee should be minimal at 99c due to (1-p) = 0.01 factor."""
        model = PolymarketFeeModel()
        order = _create_mock_order(LiquiditySide.TAKER)
        instrument = _create_mock_instrument(Decimal("0.072"))

        commission = model.get_commission(
            order, Quantity.from_str("10"), Price.from_str("0.99"), instrument,
        )

        # fee = 10 * 0.072 * 0.99 * 0.01 = 0.007128 -> rounds to 0.00713
        assert commission.as_decimal() == Decimal("0.00713")

    def test_taker_pays_reduced_fee_at_5c(self) -> None:
        """Fee should be low at 5c due to small p factor."""
        model = PolymarketFeeModel()
        order = _create_mock_order(LiquiditySide.TAKER)
        instrument = _create_mock_instrument(Decimal("0.072"))

        commission = model.get_commission(
            order, Quantity.from_str("10"), Price.from_str("0.05"), instrument,
        )

        # fee = 10 * 0.072 * 0.05 * 0.95 = 0.0342
        assert commission.as_decimal() == Decimal("0.0342")

    def test_zero_fee_rate_returns_zero(self) -> None:
        """Zero fee rate should return zero commission."""
        model = PolymarketFeeModel()
        order = _create_mock_order(LiquiditySide.TAKER)
        instrument = _create_mock_instrument(Decimal("0"))

        commission = model.get_commission(
            order, Quantity.from_str("10"), Price.from_str("0.50"), instrument,
        )

        assert commission.as_decimal() == Decimal(0)

    def test_fee_symmetry_around_50c(self) -> None:
        """Fees at 30c and 70c should be equal (symmetric around 50c)."""
        model = PolymarketFeeModel()
        order = _create_mock_order(LiquiditySide.TAKER)
        instrument = _create_mock_instrument(Decimal("0.072"))
        fill_qty = Quantity.from_str("10")

        fee_30c = model.get_commission(
            order, fill_qty, Price.from_str("0.30"), instrument,
        )
        fee_70c = model.get_commission(
            order, fill_qty, Price.from_str("0.70"), instrument,
        )

        # fee = 10 * 0.072 * 0.30 * 0.70 = 0.1512
        assert fee_30c.as_decimal() == fee_70c.as_decimal()
        assert fee_30c.as_decimal() == Decimal("0.1512")

    def test_round_trip_fees_much_lower_than_flat_model(self) -> None:
        """
        At 95c entry / 99c exit, the old MakerTakerFeeModel charged ~$1.40
        round-trip. Polymarket's real formula charges ~$0.04.
        """
        model = PolymarketFeeModel()
        order = _create_mock_order(LiquiditySide.TAKER)
        instrument = _create_mock_instrument(Decimal("0.072"))
        fill_qty = Quantity.from_str("10")

        entry_fee = model.get_commission(
            order, fill_qty, Price.from_str("0.95"), instrument,
        )
        exit_fee = model.get_commission(
            order, fill_qty, Price.from_str("0.99"), instrument,
        )
        total_fee = entry_fee.as_decimal() + exit_fee.as_decimal()

        # Old flat model: (10 * 0.95 * 0.072) + (10 * 0.99 * 0.072) = 1.3968
        flat_model_fee = Decimal("1.3968")

        # Dynamic model: 0.0342 + 0.00713 = 0.04133
        assert total_fee < Decimal("0.05")
        assert total_fee < flat_model_fee / 30  # ~34x less

    def test_fee_scales_linearly_with_quantity(self) -> None:
        """Fee should scale linearly with quantity."""
        model = PolymarketFeeModel()
        order = _create_mock_order(LiquiditySide.TAKER)
        instrument = _create_mock_instrument(Decimal("0.072"))
        fill_px = Price.from_str("0.50")

        fee_10 = model.get_commission(
            order, Quantity.from_str("10"), fill_px, instrument,
        )
        fee_100 = model.get_commission(
            order, Quantity.from_str("100"), fill_px, instrument,
        )

        assert fee_100.as_decimal() == fee_10.as_decimal() * 10
