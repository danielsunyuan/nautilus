from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def _load_crypto_5m_module():
    try:
        from nautilus_trader.adapters.polymarket.common import crypto_5m
    except (ImportError, ModuleNotFoundError):
        module_name = "nautilus_trader.adapters.polymarket.common.crypto_5m"
        module_path = (
            Path(__file__).resolve().parents[3]
            / "nautilus_trader"
            / "adapters"
            / "polymarket"
            / "common"
            / "crypto_5m.py"
        )
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    else:
        return crypto_5m


_crypto_5m = _load_crypto_5m_module()

DEFAULT_GAMMA_BASE_URL = _crypto_5m.DEFAULT_GAMMA_BASE_URL
SUPPORTED_ASSETS = _crypto_5m.SUPPORTED_ASSETS
build_forward_crypto_5m_slugs = _crypto_5m.build_forward_crypto_5m_slugs
resolve_crypto_5m_session = _crypto_5m.resolve_crypto_5m_session
