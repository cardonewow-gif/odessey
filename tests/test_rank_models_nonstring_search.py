from services.hwfit.fit import rank_models

SYS = {"has_gpu": False, "gpu_vram_gb": 0, "gpu_count": 1, "available_ram_gb": 8, "total_ram_gb": 16, "backend": "", "gpu_family": ""}


def test_rank_models_handles_non_string_search():
    # search is a query filter; a non-string made search.lower() raise
    # AttributeError. A non-string search should behave as "no filter".
    out = rank_models(SYS, search=123)
    assert isinstance(out, list)
    assert len(out) == len(rank_models(SYS))


def test_rank_models_none_search_returns_all():
    # None is also non-string; it should behave identically to "no filter".
    out = rank_models(SYS, search=None)
    assert out == rank_models(SYS)


def test_rank_models_string_filter_still_applies():
    out = rank_models(SYS, search="zzzznotarealmodelzzz")
    assert out == []
