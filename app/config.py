from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Новая БД (passengers_flow)
    app_db_host: str = "127.0.0.1"
    app_db_port: int = 3306
    app_db_user: str = "pf_user"
    app_db_password: str = ""
    app_db_name: str = "passengers_flow"

    # Старая БД (маршруты)
    routes_db_host: str = "127.0.0.1"
    routes_db_port: int = 3306
    routes_db_user: str = "routes_user"
    routes_db_password: str = ""
    routes_db_name: str = "RoutesAndAdvertisement"

    # Старая БД (трекеры / gps, пользователь usgps2)
    gps_db_host: str = "127.0.0.1"
    gps_db_port: int = 3306
    gps_db_user: str = "usgps2"
    gps_db_password: str = ""
    gps_db_name: str = "gps"

    # БД Joomla (аккаунты jos_users) — доступ через того же usgps2
    joomla_db_name: str = "joomla"

    # Медиа-сервис камер (видео/фото салона) — vcpdata
    media_base_url: str = "https://vcpdata.net.ua/activeCamersOnTooday/"
    media_timeout_sec: int = 20
    # Проверка SSL при server-side запросах к media API.
    # false — если на сервере CERTIFICATE_VERIFY_FAILED (неполная цепочка cert на vcpdata).
    media_ssl_verify: bool = True

    # Пороги алгоритма определения маршрутов по треку (межгород).
    # Значения из TrackGapFiller + разумные дефолты; можно переопределить в .env.
    route_corridor_m: int = 500       # ширина коридора маршрута
    route_min_period_sec: int = 60    # мин. длительность периода на маршруте
    route_min_period_points: int = 3  # мин. число точек в периоде

    # Внешнее API выручки (Type 1/2). Ключ передаётся заголовком X-API-Key.
    # Пустая строка = API отключено (любой запрос -> 503).
    revenue_api_key: str = "qX4vE9lkR3yaL7gyAfuF0ixQ6miGecM1sv8nzZ5oeIcnShpNasJjoHwrV2xdC2wB4khW6l"

    # Клиентские xlsx-отчёты на почту (SendLayer API). Очередь — report_deliveries,
    # отправляет демон app/workers/report_mailer.py (systemd passengersflow-mailer).
    # false — новые отчёты не ставятся в очередь и демон ничего не отправляет.
    report_emails_enabled: bool = False
    sendlayer_api_key: str = "72005294-30B6F873-7359ACB0-213D7A46"
    sendlayer_api_url: str = "https://console.sendlayer.com/api/v1/email"
    sendlayer_timeout_sec: int = 30
    # Отправитель — только на домене, верифицированном в SendLayer.
    report_from_email: str = "info@mail.vcpserver.net.ua"
    report_from_name: str = "Підрахунок пасажирів"
    report_reply_to: str = ""
    # Скрытая копия каждого отчёта (через запятую), напр. для контроля.
    report_bcc: str = ""
    # Пауза после ready до отправки: рахувальник успевает исправить ошибку.
    report_send_delay_min: int = 15
    report_max_attempts: int = 6
    report_worker_interval_sec: int = 30

    # Приложение
    secret_key: str = "change_me"
    app_host: str = "0.0.0.0"
    app_port: int = 8083
    debug: bool = True
    # Debug-лог в app/logs/debug-YYYY-MM-DD.log (from app.utils.debug_log import debug_log)
    debug_log_enabled: bool = True

    @property
    def media_base(self) -> str:
        """Базовый URL медиа-сервиса с гарантированным '/' на конце."""
        return self.media_base_url.rstrip("/") + "/"

    @property
    def app_db_url(self) -> str:
        return (
            f"mysql+pymysql://{self.app_db_user}:{self.app_db_password}"
            f"@{self.app_db_host}:{self.app_db_port}/{self.app_db_name}?charset=utf8mb4"
        )

    @property
    def routes_db_url(self) -> str:
        return (
            f"mysql+pymysql://{self.routes_db_user}:{self.routes_db_password}"
            f"@{self.routes_db_host}:{self.routes_db_port}/{self.routes_db_name}?charset=utf8mb4"
        )

    @property
    def joomla_db_url(self) -> str:
        """Подключение к БД Joomla через доступ usgps2 (блок GPS_DB)."""
        return (
            f"mysql+pymysql://{self.gps_db_user}:{self.gps_db_password}"
            f"@{self.gps_db_host}:{self.gps_db_port}/{self.joomla_db_name}?charset=utf8mb4"
        )

    @property
    def gps_db_url(self) -> str:
        """Подключение к БД gps (трекеры, last_seen) под пользователем usgps2."""
        return (
            f"mysql+pymysql://{self.gps_db_user}:{self.gps_db_password}"
            f"@{self.gps_db_host}:{self.gps_db_port}/{self.gps_db_name}?charset=utf8mb4"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
