"""Normalize incremental scope for new runs and previously saved workflows."""


def normalize_iteration_scope(value):
    # Older configurations selected noise_only. Upgrade to include pending clusters.
    if value == 'noise_only':
        value = 'unresolved_only'
    if value not in {'all', 'unresolved_only'}:
        raise ValueError('iteration_scope 必须为 all 或 unresolved_only')
    return value
