from app.providers.vk import VKProvider
from app.providers.annas import AnnasProvider
from app.providers.libgen import LibgenProvider

PROVIDERS = [
    VKProvider(),
    AnnasProvider(),
    LibgenProvider(),
]
