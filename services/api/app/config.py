from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Loaded from env / .env. Integration settings are optional until their stage lands;
    each integration checks `require()` before use so missing config fails loudly at call time."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # core
    database_url: str = "postgresql+psycopg://creatorloop:creatorloop@localhost:5432/creatorloop"
    public_base_url: str = "http://localhost:8000"
    web_base_url: str = "http://localhost:3000"
    internal_api_token: str | None = None
    signal_hash_salt: str | None = None
    admin_privy_user_ids: str = ""
    dev_auth_secret: str | None = None  # enables HS256 dev tokens when Privy is not configured
    run_indexer: bool = False
    dev_dodo: bool = False  # mock Dodo checkout + signed-webhook simulator
    dev_ipfs: bool = False  # local content store + upload routes standing in for Pinata
    ipfs_local_dir: str = ".ipfs"
    ipfs_gateway_url: str | None = None  # e.g. https://<name>.mypinata.cloud

    # chain
    amoy_rpc_url: str = "https://rpc-amoy.polygon.technology"
    publisher_private_key: str | None = None
    enforcer_private_key: str | None = None
    chain_start_block: int | None = None
    etherscan_api_key: str | None = None

    # privy
    privy_app_id: str | None = None
    privy_verification_key: str | None = None

    # tigergraph
    tg_host: str | None = None
    tg_graph: str = "CreatorLoop"
    tg_secret: str | None = None
    tg_username: str | None = None
    tg_password: str | None = None

    # dodo
    dodo_payments_api_key: str | None = None
    dodo_payments_webhook_secret: str | None = None
    dodo_environment: str = "test_mode"
    dodo_product_supporter_pass: str | None = None
    dodo_product_boost_pack: str | None = None

    # viasocket
    viasocket_signing_secret: str | None = None
    viasocket_webhook_payment: str | None = None
    viasocket_webhook_fraud: str | None = None
    viasocket_webhook_ledger: str | None = None
    viasocket_webhook_digest: str | None = None
    local_scheduler: bool = False
    viasocket_local_sink: bool = False  # write events to a JSONL file instead of POSTing to viaSocket
    viasocket_sink_path: str = ".viasocket-sink.jsonl"

    # ai
    ai_base_url: str | None = None
    ai_api_key: str | None = None
    ai_model_investigator: str | None = None
    ai_model_moderator: str | None = None
    ai_model_copilot: str | None = None
    smallest_api_key: str | None = None

    # pinata
    pinata_jwt: str | None = None

    # rewards
    epoch_pool_loop_wei: int = 10_000 * 10**18
    creator_fund_share_bps: int = 7000
    epoch_window_minutes: int = 30  # demo; production uses 10080 (7 days)
    epoch_creator_cap_bps: int = 1000  # no creator gets more than 10% of an epoch

    def require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            raise RuntimeError(f"missing configuration: {', '.join(n.upper() for n in missing)}")

    @property
    def admin_ids(self) -> set[str]:
        return {x.strip() for x in self.admin_privy_user_ids.split(",") if x.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
