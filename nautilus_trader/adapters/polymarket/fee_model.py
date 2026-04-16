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
Polymarket-specific fee model implementing the dynamic fee formula.

Polymarket charges fees using the formula: fee = C * feeRate * p * (1 - p)
where:
- C is the number of shares
- feeRate is the effective taker rate from the market's feeSchedule
- p is the share price

Fees peak at p=0.5 (50c) and decrease symmetrically toward both extremes.
Only takers pay fees; makers are always charged zero.

References
----------
https://docs.polymarket.com/trading/fees

"""

from decimal import Decimal

from nautilus_trader.backtest.models import FeeModel
from nautilus_trader.model.enums import LiquiditySide
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Money
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.orders import Order


class PolymarketFeeModel(FeeModel):
    """
    Polymarket fee model implementing the dynamic fee formula.

    The formula is: fee = quantity * price * fee_rate * (1 - price)

    This produces fees that peak at 50c (p=0.5) and decrease toward
    both 0c and 100c extremes. Only takers pay fees.

    """

    def get_commission(
        self,
        order: Order,
        fill_qty: Quantity,
        fill_px: Price,
        instrument: Instrument,
    ) -> Money:
        """
        Calculate the Polymarket commission for a fill.

        Parameters
        ----------
        order : Order
            The order being filled.
        fill_qty : Quantity
            The fill quantity (shares).
        fill_px : Price
            The fill price.
        instrument : Instrument
            The instrument for the order.

        Returns
        -------
        Money
            The commission amount in the instrument's quote currency.

        """
        # Makers pay zero fees
        if order.liquidity_side == LiquiditySide.MAKER:
            return Money(0, instrument.quote_currency)

        # Get the fee rate from the instrument
        fee_rate = instrument.taker_fee
        if fee_rate == 0:
            return Money(0, instrument.quote_currency)

        # Apply Polymarket's dynamic formula: fee = C * rate * p * (1 - p)
        qty = fill_qty.as_decimal()
        price = fill_px.as_decimal()

        # Calculate commission: qty * fee_rate * price * (1 - price)
        commission_value = qty * fee_rate * price * (Decimal(1) - price)

        # Round to 5 decimal places (Polymarket's minimum fee precision)
        commission_value = round(commission_value, 5)

        return Money(commission_value, instrument.quote_currency)
