"""FMBSM desktop client for contributing Microsoft Copilot sessions."""

try:
    from ._build_version import VERSION as __version__
except ImportError:
    __version__ = "1.0.0-dev"
