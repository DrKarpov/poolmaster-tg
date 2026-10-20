from display_names import display_pair_name
from farming import FARMING_POOLS, TON_MASTER_ADDRESS


def test_ton_pairs_are_gram_only_in_display_labels():
    assert display_pair_name(FARMING_POOLS[0]) == "JETTON/GRAM"
    assert display_pair_name(FARMING_POOLS[2]) == "JETTON/GRAM"
    assert display_pair_name(FARMING_POOLS[1]) == "JETTON/USDT"
    assert display_pair_name(FARMING_POOLS[3]) == "JETTON/USDT"

    assert FARMING_POOLS[0].name == "JETTON/TON"
    assert FARMING_POOLS[2].name == "JETTON/TON"
    assert TON_MASTER_ADDRESS == "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c"
