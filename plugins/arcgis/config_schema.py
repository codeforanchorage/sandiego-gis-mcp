"""Pydantic configuration schema for the ArcGIS Enterprise portal plugin."""

from typing import Optional
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _validate_http_url(v: str) -> str:
    result = urlparse(v)
    if not result.scheme or not result.netloc:
        raise ValueError("URL must include scheme (http/https) and hostname")
    if result.scheme not in ("http", "https"):
        raise ValueError("URL scheme must be http or https")
    return v.rstrip("/")


class ArcGISPluginConfig(BaseModel):
    """Configuration schema for the ArcGIS Enterprise portal plugin.

    This schema validates ArcGIS plugin configuration from config.yaml.
    At least one of `portal_url` (catalog search) or `services_url`
    (services-directory walk) must be set.
    """

    enabled: bool = Field(default=False, description="Whether plugin is enabled")
    portal_url: str = Field(
        default="",
        description=(
            "Base URL of the ArcGIS Enterprise portal "
            "(e.g., https://geo.sandag.org/portal). Used for catalog search "
            "at <portal_url>/sharing/rest/search when open to anonymous "
            "callers."
        ),
    )
    services_url: str = Field(
        default="",
        description=(
            "Base URL of the ArcGIS Server services directory "
            "(e.g., https://geo.sandag.org/server/rest/services). Used as "
            "the discovery fallback when portal search is unavailable, and "
            "to resolve service-path dataset IDs."
        ),
    )
    geocoder_url: str = Field(
        default="",
        description=(
            "Optional ArcGIS GeocodeServer URL for findAddressCandidates "
            "(e.g., https://gis.sandag.org/sdgis/rest/services/"
            "SANDAG_COMPOSITE_LOCATOR/GeocodeServer). When unset, the US "
            "Census geocoder is used."
        ),
    )
    city_name: str = Field(..., description="Name of the city/organization")
    timeout: int = Field(
        default=120, ge=1, le=300, description="HTTP request timeout in seconds"
    )
    token: Optional[str] = Field(
        None, description="Optional Bearer token for authenticated requests"
    )
    geocoder_region: str = Field(
        default="",
        description=(
            "Optional region (e.g. 'San Diego, CA') appended to addresses "
            "during US Census geocoding to bias results to this jurisdiction. "
            "Not used with an ArcGIS geocoder_url (regional locators do not "
            "need biasing)."
        ),
    )

    @field_validator("portal_url", "services_url", "geocoder_url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        """Validate that URL is well-formed (empty allowed; see model check)."""
        if not v:
            return v
        try:
            return _validate_http_url(v)
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"Invalid URL format: {e}")

    @model_validator(mode="after")
    def require_a_discovery_endpoint(self) -> "ArcGISPluginConfig":
        if not self.portal_url and not self.services_url:
            raise ValueError(
                "At least one of portal_url or services_url must be configured"
            )
        return self

    model_config = ConfigDict(extra="forbid")
