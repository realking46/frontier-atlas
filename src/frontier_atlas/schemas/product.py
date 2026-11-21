from __future__ import annotations

from enum import Enum
from typing import ClassVar, Literal

from pydantic import BaseModel

from .base import BaseRecord, ExtractionContent, grounded, inferred


class PricingModel(str, Enum):
    FREE = "FREE"
    FREEMIUM = "FREEMIUM"
    PAID = "PAID"
    ENTERPRISE = "ENTERPRISE"


class ProductExtraction(ExtractionContent):
    prompt_name: ClassVar[str] = "product"

    productName: str | None = grounded("Product name exactly as written")
    startupName: str | None = grounded("Company that makes the product, as written")
    description: str | None = inferred("One-sentence description")
    website: str | None = grounded("Product URL if shown")
    pricingModel: PricingModel | None = inferred("FREE, FREEMIUM, PAID or ENTERPRISE")
    categories: list[str] | None = inferred("Category tags")


class ProductContent(BaseModel):
    productName: str
    startupName: str | None = None
    rawStartupName: str | None = None
    pricingModel: PricingModel | None = None
    pricingRaw: str | None = None              # the source's own pricing label(s), verbatim
    startupNameSource: str | None = None        # how the maker was established (see crawlers/bulk/makers.py)
    startupEvidenceUrl: str | None = None       # page the maker name was read from
    description: str | None = None
    website: str | None = None
    categories: list[str] = []


class ProductRecord(BaseRecord):
    recordType: Literal["PRODUCT"] = "PRODUCT"
    content: ProductContent
