"""Select one exact provider/model option advertised by OpenCode ACP."""

from .errors import OpenCodeError
from .protocol import explicit_model


def catalog_ids(options):
    ids = set()
    for value in options:
        try:
            ids.add(explicit_model(value))
        except OpenCodeError:
            continue
    return sorted(ids)


def resolve_model(requested, options):
    explicit_model(requested)
    ids = catalog_ids(options)
    if requested not in ids:
        raise ModelResolutionError(ids)
    return requested


class ModelResolutionError(OpenCodeError):
    def __init__(self, ids):
        super().__init__("model_unavailable")
        self.args = (f"{self.args[0]} 可用 ID：{', '.join(ids[:30]) or '无'}",)
