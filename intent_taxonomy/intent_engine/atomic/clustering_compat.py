"""Bridge old clustering callers to scikit-learn's renamed validation flag."""
from functools import wraps
from importlib import import_module
from inspect import signature


def adapt_check_array(module):
    """Adapt only a clustering module's alias, never sklearn's global function."""
    check_array = getattr(module, "check_array", None)
    if check_array is None:
        return
    parameters = signature(check_array).parameters
    if "force_all_finite" in parameters or "ensure_all_finite" not in parameters:
        return
    if getattr(check_array, "_intent_finite_compat", False):
        return

    @wraps(check_array)
    def compatible(*args, **kwargs):
        if "force_all_finite" in kwargs:
            if "ensure_all_finite" in kwargs:
                raise TypeError("Cannot pass both force_all_finite and ensure_all_finite")
            kwargs["ensure_all_finite"] = kwargs.pop("force_all_finite")
        return check_array(*args, **kwargs)

    compatible._intent_finite_compat = True
    module.check_array = compatible


def configure_clustering_compat():
    # Older HDBSCAN and UMAP import check_array into their own module namespace.
    for name in ("hdbscan.hdbscan_", "hdbscan.prediction", "umap.umap_"):
        adapt_check_array(import_module(name))
