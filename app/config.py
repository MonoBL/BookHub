from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # core
    DATA_DIR: str = "/data"
    # HOST_DATA_DIR: host-side path that the daemon sees for the /data volume.
    # Required for docker-out-of-docker: -v bind-mounts are resolved by the
    # daemon on the HOST, not inside the app container. Set to the host path
    # that maps to DATA_DIR (e.g. /opt/bookhub/data). Leave empty for local dev.
    HOST_DATA_DIR: str = ""
    COOKIE_SECURE: bool = False
    ADMIN_PASSWORD: str = ""
    CLOUDFLARED_TOKEN: str = ""
    # Expose Swagger/OpenAPI (/docs, /redoc, /openapi.json). Off in prod: the
    # schema maps every route incl. admin. Enable only for local development.
    ENABLE_DOCS: bool = False
    # Session cookie lifetime (days). Shorter = smaller stolen-cookie window.
    SESSION_TTL_DAYS: int = 7

    # limits
    DOWNLOAD_MAX_MB: int = 32
    CONVERT_MAX_MB: int = 200
    IMAGE_MAX_MB: int = 50           # per-batch cap for image->BMP uploads
    BMP_MAX_FILES: int = 200         # max images per BMP batch
    BMP_DEFAULT_WIDTH: int = 480     # XTeink X4 portrait width
    BMP_DEFAULT_HEIGHT: int = 800    # XTeink X4 portrait height
    # File retention: a prepared download is kept this long, then deleted even
    # if never grabbed. Downloading also deletes immediately. See cleaner.py.
    FILE_TTL_MINUTES: int = 30
    DOWNLOAD_CONCURRENCY: int = 3
    SCAN_CONCURRENCY: int = 3
    CONVERT_CONCURRENCY: int = 1

    # VirusTotal
    VT_API_KEY: str = ""
    VT_CLEAN_MAX_AGE_DAYS: int = 180
    VT_MIN_ENGINES: int = 40
    VT_DAILY_CAP: int = 480

    # providers
    VK_TOKEN: str = ""
    AA_API_KEY: str = ""
    LIBGEN_MIRRORS: str = "libgen.la,libgen.li,libgen.vg,libgen.gl"
    PROVIDER_SEARCH_TIMEOUT_S: int = 15
    PROVIDER_RESOLVE_TIMEOUT_S: int = 30

    # converter sandbox
    # Production: RUNSC_RUNTIME=runsc (gVisor, see BUILD.md §8).
    # Local dev on macOS (no gVisor): set RUNSC_RUNTIME="" in .env to use
    # plain "docker run" without --runtime. Keep runsc as the default.
    RUNSC_RUNTIME: str = "runsc"
    CONVERTER_IMAGE: str = "bookhub-converter:latest"
    CONVERT_TIMEOUT_S: int = 600
    OCR_TIMEOUT_S: int = 1200
    # Comic / scanned mode: image-heavy PDFs OCR every page, so allow much longer.
    COMIC_CONVERT_TIMEOUT_S: int = 2400
    COMIC_OCR_TIMEOUT_S: int = 3600
    OCR_LANGS: str = "eng+por+fra+spa"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
