from .base import Provider, ProviderUnavailable, Result
from .google_direct import GoogleDirectProvider
from .ignav import IgnavProvider
from .serpapi import SerpApiProvider
from .travelpayouts import TravelpayoutsProvider

REGISTRY: dict[str, type[Provider]] = {
    p.name: p for p in (SerpApiProvider, IgnavProvider, TravelpayoutsProvider, GoogleDirectProvider)
}

__all__ = ["REGISTRY", "Provider", "ProviderUnavailable", "Result"]
