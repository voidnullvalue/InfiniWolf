"""InfiniWolf campaign generator."""

__version__ = "1.7.0"

from .config import CampaignConfig, Intensity, ThemeBias
from .generator import generate_campaign

__all__ = ["CampaignConfig", "Intensity", "ThemeBias", "generate_campaign"]
