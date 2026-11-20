"""Order placement."""


def place_order(order):
    """Place an order."""
    if order is None:
        raise ValueError("order required")
    return order
