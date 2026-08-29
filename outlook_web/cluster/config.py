from dataclasses import dataclass
import os


class ClusterConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ClusterConfig:
    role: str
    master_url: str
    poll_seconds: int
    max_stale_seconds: int
    event_retention_days: int

    @property
    def is_primary(self):
        return self.role == 'primary'

    @property
    def is_replica(self):
        return self.role == 'replica'


def _bounded_int(name, default, minimum, maximum):
    raw = str(os.getenv(name, default)).strip()
    if not raw.isdigit() or not minimum <= int(raw) <= maximum:
        raise ClusterConfigError(f'{name} must be between {minimum} and {maximum}')
    return int(raw)


def load_cluster_config():
    role = str(os.getenv('NODE_ROLE', 'primary')).strip().lower() or 'primary'
    if role not in {'primary', 'replica'}:
        raise ClusterConfigError('NODE_ROLE must be primary or replica')
    master_url = str(os.getenv('MASTER_URL', '')).strip().rstrip('/')
    if role == 'replica' and not master_url:
        raise ClusterConfigError('MASTER_URL is required for replica nodes')
    return ClusterConfig(
        role=role,
        master_url=master_url,
        poll_seconds=_bounded_int('REPLICA_POLL_SECONDS', 10, 5, 60),
        max_stale_seconds=_bounded_int('REPLICA_MAX_STALE_SECONDS', 86400, 0, 2592000),
        event_retention_days=_bounded_int('REPLICATION_EVENT_RETENTION_DAYS', 30, 1, 365),
    )
