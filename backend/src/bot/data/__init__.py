from bot.data.feed_mock import MockFeed
from bot.data.feed_rest import KalshiRestFeed
from bot.data.feed_ws import KalshiWsFeed
from bot.data.http_client import KalshiHttpClient


def build_feed(cfg):
    """
    Factory: return the correct DataFeed implementation based on config.

    config.yaml:
        data_feed:
          mode: "rest_poll"   → KalshiRestFeed
          mode: "websocket"   → KalshiWsFeed
    """
    mode = cfg.data_feed.mode
    if mode == "rest_poll":
        return KalshiRestFeed(cfg.kalshi, cfg.data_feed)
    elif mode == "websocket":
        return KalshiWsFeed(cfg.kalshi, cfg.data_feed)
    else:
        raise ValueError(f"Unknown data_feed.mode: {mode!r}")


__all__ = ["KalshiRestFeed", "KalshiWsFeed", "MockFeed", "KalshiHttpClient", "build_feed"]
