"""Random Wolf campaign generator."""

__version__ = "0.1.0"

from .config import CampaignConfig, Intensity
from .generator import generate_campaign

__all__ = ["CampaignConfig", "Intensity", "generate_campaign"]
