from farming import FarmingPool


def display_pair_name(pool: FarmingPool) -> str:
    """Return the public pair label without changing the configured pool identity."""
    if pool.name == "JETTON/TON":
        return "JETTON/GRAM"
    return pool.name
