"""Exception types raised by flow-doctor.

All flow-doctor configuration and runtime errors inherit from FlowDoctorError.
Callers can catch the base class to handle any flow-doctor failure, or catch
specific subclasses for targeted handling.

flow-doctor fails loud by default on MISCONFIGURATION. If a notifier is
misconfigured, if a required environment variable is missing, or if the
store config names an unsupported type, a subclass of FlowDoctorError is
raised (respecting ``strict``) — this is intentional, since silent
degradation means users discover broken error monitoring only during an
incident, which defeats the purpose.

``StorageBackendError`` is the one exception: it signals a RUNTIME/infra
failure (not misconfiguration) from the storage backend actually initializing
— e.g. an IAM permission gap, DynamoDB throttling, a network blip. Unlike
every other FlowDoctorError subclass, it is always caught and degraded inside
``FlowDoctor.__init__``, regardless of ``strict`` — see its docstring.
"""

from __future__ import annotations


class FlowDoctorError(Exception):
    """Base class for all flow-doctor errors."""


class ConfigError(FlowDoctorError):
    """Raised when flow-doctor configuration is invalid or incomplete.

    Common causes:
        - A notifier is listed in config but required fields (token, webhook, etc.) are missing
        - A ``${VAR}`` reference in YAML cannot be resolved from the environment
        - Zero notifiers are configured (flow-doctor has no way to surface errors)

    The error message names the specific field and suggests which environment
    variable to set. See the FLOW_DOCTOR_* env var contract in the README.
    """


class StorageBackendError(FlowDoctorError):
    """Raised when a storage backend fails at runtime while flow-doctor is initializing.

    Distinct from ``ConfigError``: this covers infra/environment failures from
    actually calling the backend (an IAM permission gap, DynamoDB throttling,
    a network blip) rather than a config mistake (missing table_name, bad
    store type). ``FlowDoctor.__init__`` always treats this as non-fatal,
    regardless of ``strict`` — flow-doctor is a telemetry side-channel
    instrumenting its caller, and a transient failure in its OWN backend must
    never be allowed to crash the producer it was only trying to log for.
    ``strict`` still governs genuine misconfiguration (missing install/config/
    secret/table_name) exactly as before; it was never meant to license this
    failure mode. See nousergon/alpha-engine-config#2465.
    """
