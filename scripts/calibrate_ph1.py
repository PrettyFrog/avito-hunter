#!/usr/bin/env python3
"""PH-1 Photo Verification Calibration Script.

Evaluates Method A, Method B, and the PH-1 decision rule on known positive and negative
lots stored in data/images/ and database feedback.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import sys

from loguru import logger

from hunter.core.config import get_settings
from hunter.core.db import Database
from hunter.core.models import Card
from hunter.core.product import ProductStore
from hunter.vision.reference import list_reference_photos, get_signature_version
from hunter.vision.verifier import (
    ALLOWED_CATEGORIES,
    evaluate_ph1_decision,
    run_ph1_method_a,
    run_ph1_method_b,
)
from hunter.llm.router import LLMRouter


@dataclass
class CalibrationSample:
    avito_id: str
    product_id: str
    ground_truth: str  # "positive" (target model) or "negative" (wrong model/replica)
    notes: str = ""


# Labeled corpus from data/images/ and real Avito observations
CORPUS: list[CalibrationSample] = [
    # Negatives: replica / older generation / wrong brand / other models
    CalibrationSample(
        avito_id="8308064174",
        product_id="gem_headphones_bose",
        ground_truth="negative",
        notes="Replica / distinct fold mechanism",
    ),
    CalibrationSample(
        avito_id="8308234572",
        product_id="gem_headphones_bose",
        ground_truth="negative",
        notes="Wrong model / QC35",
    ),
    CalibrationSample(
        avito_id="8308248473",
        product_id="gem_headphones_bose",
        ground_truth="negative",
        notes="Wrong model / NC700",
    ),
    CalibrationSample(
        avito_id="8395731914",
        product_id="sony_xm6",
        ground_truth="negative",
        notes="Excluded replica / other model",
    ),
    # Positives: confirmed targets
    CalibrationSample(
        avito_id="8283322300",
        product_id="gem_headphones_sony",
        ground_truth="positive",
        notes="Confirmed Sony target",
    ),
    CalibrationSample(
        avito_id="8283735775",
        product_id="gem_headphones_sony",
        ground_truth="positive",
        notes="Confirmed Sony target",
    ),
    CalibrationSample(
        avito_id="8268397751",
        product_id="gem_headphones_bose",
        ground_truth="positive",
        notes="Confirmed Bose target",
    ),
]


async def run_calibration() -> None:
    settings = get_settings()
    store = ProductStore()
    router = LLMRouter()
    db = Database(settings.db_path)
    await db.connect()

    print("\n" + "=" * 90)
    print("🔬 PH-1 PHOTO VERIFICATION CALIBRATION REPORT")
    print("=" * 90)
    print(f"{'Avito ID':<12} | {'Product ID':<20} | {'True':<8} | {'Method A':<10} | {'Method B':<10} | {'PH-1':<8} | {'Cat Diffs'}")
    print("-" * 90)

    total_samples = 0
    positives = 0
    negatives = 0
    false_positives = 0  # Negative flagged as confirmed or pass
    false_negatives = 0  # Positive mistakenly skipped (FATAL)
    true_skips = 0

    for sample in CORPUS:
        p = store.get(sample.product_id)
        if not p:
            continue

        lot_dir = settings.images_dir / sample.avito_id
        if not lot_dir.exists():
            continue

        images = [f for f in sorted(lot_dir.glob("*.jpg")) if f.name != "collage.jpg"]
        if not images:
            continue

        total_samples += 1
        if sample.ground_truth == "positive":
            positives += 1
        else:
            negatives += 1

        ref_photos = list_reference_photos(p.id, negative=False)
        if not ref_photos and getattr(p.photos, "reference_product", None):
            ref_photos = list_reference_photos(p.photos.reference_product, negative=False)

        sig_ver = get_signature_version(p.id)
        photo_results = []

        # Check up to 3 images
        for idx, img_path in enumerate(images[:3]):
            try:
                res_a = await run_ph1_method_a(
                    img_path, p, router=router, db=db, avito_id=sample.avito_id, signature_version=sig_ver
                )
                res_b = await run_ph1_method_b(
                    img_path, ref_photos, p, router=router, db=db, avito_id=sample.avito_id, signature_version=sig_ver
                )
                visible = res_a.get("product_visible", True) and res_b.get("product_visible", True)
                photo_results.append({
                    "photo_index": idx,
                    "product_visible": visible,
                    "method_a": res_a,
                    "method_b": res_b,
                })
            except Exception as exc:
                logger.warning("Error evaluating {} photo {}: {}", sample.avito_id, img_path.name, exc)

        verdict, status_line, skip_reason = evaluate_ph1_decision(
            photo_results, photo_check_mode="strict", vlm_available=True
        )

        # Collect summary diffs
        diffs = []
        for pr in photo_results:
            for m in (pr.get("method_a", {}), pr.get("method_b", {})):
                for d in m.get("categorical_differences", []):
                    cat = d.get("category")
                    if cat in ALLOWED_CATEGORIES and cat not in diffs:
                        diffs.append(cat)

        res_a_summary = photo_results[0]["method_a"].get("same_model", "?") if photo_results else "—"
        res_b_summary = photo_results[0]["method_b"].get("same_model", "?") if photo_results else "—"

        print(
            f"{sample.avito_id:<12} | {sample.product_id:<20} | {sample.ground_truth:<8} | "
            f"{res_a_summary:<10} | {res_b_summary:<10} | {verdict:<8} | {', '.join(diffs) or 'none'}"
        )

        if sample.ground_truth == "positive" and verdict == "skip":
            false_negatives += 1
            print(f"  ❌ CRITICAL FALSE NEGATIVE on positive sample {sample.avito_id}!")
        elif sample.ground_truth == "negative" and verdict == "skip":
            true_skips += 1

    print("-" * 90)
    print(f"Total evaluated samples: {total_samples} (Positives: {positives}, Negatives: {negatives})")
    print(f"Strict skips on negatives: {true_skips}/{negatives}")
    print(f"False Negatives on positives: {false_negatives} (MUST BE 0)")
    print("=" * 90 + "\n")

    await db.close()

    if false_negatives > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run_calibration())
