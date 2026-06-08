from app.providers.vk import VKProvider
from app.providers.annas import AnnasProvider
from app.providers.libgen import LibgenProvider
from app.providers.archive import ArchiveProvider

PROVIDERS = [
    VKProvider(),
    AnnasProvider(),
    LibgenProvider(),
    ArchiveProvider(),
]
