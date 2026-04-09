from bot.execution.convert import signal_to_order
from bot.execution.paper import PaperEngine
from bot.execution.kalshi import KalshiEngine


def build_engine(cfg):
    """
    Factory: return the correct ExecutionEngine based on config.

    config.yaml:
        execution:
          mode: "paper"  → PaperEngine (safe default)
          mode: "live"   → KalshiEngine (real money — use with caution)
    """
    from bot.data.http_client import KalshiHttpClient

    mode = cfg.execution.mode
    if mode == "paper":
        return PaperEngine(
            fill_delay_seconds=cfg.execution.paper_fill_delay_seconds,
            slippage_cents=cfg.execution.paper_slippage_cents,
        )
    elif mode == "live":
        client = KalshiHttpClient(
            base_url=cfg.kalshi.base_url,
            api_key=cfg.kalshi.api_key,
            api_secret=cfg.kalshi.api_secret,
            timeout_seconds=cfg.kalshi.request_timeout_seconds,
            max_retries=cfg.kalshi.max_retries,
            retry_backoff_seconds=cfg.kalshi.retry_backoff_seconds,
        )
        return KalshiEngine(http_client=client)
    else:
        raise ValueError(f"Unknown execution.mode: {mode!r}")


__all__ = ["PaperEngine", "KalshiEngine", "signal_to_order", "build_engine"]
