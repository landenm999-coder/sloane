"""Provider implementations. Nothing outside this package imports a vendor SDK.

Import providers through sloane.router, never from here directly. The router is
the upgrade lever; bypassing it is what makes a model swap a code change.
"""

from sloane.providers.base import Provider, ProviderError, Usage

__all__ = ["Provider", "ProviderError", "Usage"]
