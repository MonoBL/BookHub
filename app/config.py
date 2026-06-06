from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # core
    DATA_DIR: str = "/data"
    COOKIE_SECURE: bool = False
    ADMIN_PASSWORD: str = ""
    CLOUDFLARED_TOKEN: str = ""

    # limits
    DOWNLOAD_MAX_MB: int = 32
    CONVERT_MAX_MB: int = 200
    FILE_TTL_MINUTES: int = 60
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
    OCR_LANGS: str = "eng+por+fra+spa"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
