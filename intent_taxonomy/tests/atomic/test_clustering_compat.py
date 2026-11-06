from types import SimpleNamespace

import numpy as np
import pytest
from sklearn.utils import check_array

from intent_engine.atomic.clustering_compat import adapt_check_array


def modern_check_array(values, *, ensure_all_finite=True, accept_sparse=False):
    return check_array(values, ensure_all_finite=ensure_all_finite, accept_sparse=accept_sparse)


def test_legacy_keyword_keeps_finite_validation_semantics():
    module = SimpleNamespace(check_array=modern_check_array)
    adapt_check_array(module)
    values = [[1.0, np.nan]]
    assert np.isnan(module.check_array(values, accept_sparse="csr", force_all_finite=False)[0, 1])
    with pytest.raises(ValueError):
        module.check_array(values, force_all_finite=True)
    assert np.isnan(module.check_array(values, force_all_finite="allow-nan")[0, 1])
    with pytest.raises(ValueError):
        module.check_array([[np.inf]], force_all_finite="allow-nan")
    assert module.check_array([[1]], ensure_all_finite=True)[0, 0] == 1
    assert module.check_array is not modern_check_array


def test_adapter_is_idempotent_and_rejects_ambiguous_flags():
    module = SimpleNamespace(check_array=modern_check_array)
    adapt_check_array(module)
    first = module.check_array
    adapt_check_array(module)
    assert module.check_array is first
    with pytest.raises(TypeError, match="both"):
        module.check_array([[1]], force_all_finite=False, ensure_all_finite=True)
    with pytest.raises(TypeError):
        module.check_array([[1]], unknown_parameter=True)


def test_old_sklearn_alias_is_left_untouched():
    def old_check_array(values, *, force_all_finite=True):
        return values, force_all_finite
    module = SimpleNamespace(check_array=old_check_array)
    adapt_check_array(module)
    assert module.check_array is old_check_array
