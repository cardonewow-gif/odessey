from services.hwfit.fit import rank_models


def test_rank_models_handles_non_dict_system():
    # `system` is the detected-hardware dict; if detection failed and returned
    # None (or a non-dict), system.get(...) raised AttributeError. Treat a
    # non-dict system as "unknown hardware" (no GPU).
    assert isinstance(rank_models(None), list)
    assert isinstance(rank_models(123), list)
