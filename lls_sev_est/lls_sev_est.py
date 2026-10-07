
"""
lls_sev_est.py
Utilities to estimate lesion severity on leaves (K-Means + defoliation adjustment),
with optional step-by-step logs, edge protection at leaflet borders, and extended visualization.
Includes helpers to save figures and results.
"""
from pathlib import Path
import os
import cv2
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from sklearn import preprocessing
from sklearn.cluster import KMeans
from skimage.feature import local_binary_pattern

__all__ = ["lls_sev_est", "save_figure", "save_results"]
__version__ = "0.5.2"


def lls_sev_est(
    image_route: str,
    nodes: int,
    results_df: pd.DataFrame,
    min_area: int = 800,
    max_area: int | None = None,
    dark_threshold: int = 100,
    show_results: bool = False,
    verbose: bool = True,
    return_figure: bool = False,
    edge_protection_px: int = 7,
):
    """
    Estimate lesion severity using K-Means and compute multiplicative severity adjusted by defoliation.
    Adds *edge protection* by eroding the leaflet mask so border pixels don't bias severity.

    If `show_results=True`, displays a 3-panel visualization (original, detected leaflets, K-Means segmentation)
    plus a summary panel. Optionally returns the Matplotlib figure.

    Parameters
    ----------
    image_route : str
        Path to input image.
    nodes : int
        Number of nodes (expected leaflets = nodes * 4).
    results_df : pd.DataFrame
        External DataFrame where a new row will be appended with the results.
    min_area : int, optional
        Minimum contour area to be considered a leaflet.
    max_area : int | None, optional
        Maximum contour area; if None, no upper bound is applied.
    dark_threshold : int, optional
        Mean grayscale threshold to discard overly dark regions when counting leaflets.
    show_results : bool, optional
        If True, displays the figures.
    verbose : bool, optional
        If True, prints step-by-step progress logs.
    return_figure : bool, optional
        If True and show_results=True, returns (results_df, fig). Otherwise returns only `results_df`.
    edge_protection_px : int, optional
        Number of pixels to erode from leaflet borders when computing severity (default 7).

    Returns
    -------
    pd.DataFrame
        The `results_df` with an additional row.
    tuple(pd.DataFrame, matplotlib.figure.Figure)  optional
        If `return_figure=True` and `show_results=True`.
    """
    def log(msg: str):
        if verbose:
            print(msg)

    # --- Read image ---
    img_bgr = cv2.imread(image_route, cv2.IMREAD_UNCHANGED)
    if img_bgr is None:
        raise FileNotFoundError(f"Could not read image at {image_route}")
    # For TIFF with alpha or extra channels, keep first 3 channels (BGR)
    if img_bgr.ndim == 3 and img_bgr.shape[2] > 3:
        img_bgr = img_bgr[:, :, :3]
    h, w = img_bgr.shape[:2]
    log("🔍 Starting leaf severity analysis...")
    log(f"✅ Image loaded: {h}x{w} (BGR)")

    # --- Binary mask (Otsu) + morphology ---
    log("🧪 Building binary mask (Otsu) and running morphological cleanup...")
    img_rgb_orig = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 1.5)

    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    kernel3 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel3, iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel3, iterations=1)
    log("✅ Thresholding and morphology completed")

    # --- Contours / leaflet filtering ---
    log("🔎 Finding contours and filtering leaflets by area and brightness...")
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    leaflets = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < min_area:
            continue
        if max_area is not None and area > max_area:
            continue

        cnt_mask = np.zeros(mask.shape, dtype=np.uint8)
        cv2.drawContours(cnt_mask, [c], -1, 255, thickness=cv2.FILLED)
        mean_val = cv2.mean(gray, mask=cnt_mask)[0]

        if mean_val < dark_threshold:  # discard very dark regions
            continue

        leaflets.append(c)

    n_leaflets = len(leaflets)
    expected_leaflets = nodes * 4
    missing_leaflets = expected_leaflets - n_leaflets
    log(f"✅ Leaflets detected: {n_leaflets} (expected: {expected_leaflets}, missing: {max(0, missing_leaflets)})")

    # Build leaflet mask and *edge-protected* ROI
    leaflet_mask = np.zeros_like(mask, dtype=np.uint8)
    if len(leaflets) > 0:
        cv2.drawContours(leaflet_mask, leaflets, -1, 255, thickness=cv2.FILLED)

    # Erode mask to protect borders from being counted as lesions
    if edge_protection_px > 0:
        ksize = max(1, int(edge_protection_px) * 2 + 1)
        kernel_e = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ksize, ksize))
        roi_mask = cv2.erode(leaflet_mask, kernel_e)
        # Fallback: if erosion removed too much, reduce erosion
        if np.count_nonzero(roi_mask) < 0.1 * max(1, np.count_nonzero(leaflet_mask)):
            ksize2 = max(1, int(max(1, edge_protection_px // 2)) * 2 + 1)
            kernel_e2 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ksize2, ksize2))
            roi_mask = cv2.erode(leaflet_mask, kernel_e2)
            log(f"⚠️ Edge protection reduced to ~{max(1, edge_protection_px // 2)} px due to small ROI.")
    else:
        roi_mask = leaflet_mask.copy()
    roi_area = int(np.count_nonzero(roi_mask))
    log(f"🛡️ Edge protection applied: {edge_protection_px}px (ROI pixels: {roi_area:,})")

    out_bgr = img_bgr.copy()
    if len(leaflets) > 0:
        cv2.drawContours(out_bgr, leaflets, -1, (0, 255, 0), 2)
    out_rgb = cv2.cvtColor(out_bgr, cv2.COLOR_BGR2RGB)

    # -------------------------
    # K-Means segmentation
    # -------------------------
    log("🧰 Preparing features for K-Means (RGB, gradients, distance, RGB normalization, LBP)...")
    img_rgb = img_rgb_orig.copy()
    img_blur = cv2.medianBlur(img_rgb, 11)
    dims = img_blur.shape
    rows, cols = dims[0], dims[1]
    n_pixels = rows * cols
    ind = np.indices((rows, cols))
    data = np.zeros([n_pixels, 5], dtype="int")
    data[:, 0] = ind[0].flatten()
    data[:, 1] = ind[1].flatten()
    data[:, 2] = img_blur[:, :, 0].flatten()
    data[:, 3] = img_blur[:, :, 1].flatten()
    data[:, 4] = img_blur[:, :, 2].flatten()
    df = pd.DataFrame(data, columns=["row", "col", "red", "green", "blue"])

    df_feat = df.copy()
    df_feat["diff_r_g"] = df_feat["red"] - df_feat["green"]
    df_feat["diff_r_b"] = df_feat["red"] - df_feat["blue"]
    df_feat["diff_b_g"] = df_feat["blue"] - df_feat["green"]

    gray2 = cv2.cvtColor(img_blur, cv2.COLOR_RGB2GRAY)
    grad_x = cv2.Sobel(gray2, cv2.CV_64F, 1, 0, ksize=5)
    grad_y = cv2.Sobel(gray2, cv2.CV_64F, 0, 1, ksize=5)
    magnitude = np.sqrt(grad_x**2 + grad_y**2)
    df_feat["gradient"] = magnitude.flatten()

    center_row, center_col = rows / 2, cols / 2
    df_feat["dist_to_center"] = np.sqrt(
        (df_feat["row"] - center_row) ** 2 + (df_feat["col"] - center_col) ** 2
    )

    # Avoid division by zero
    sum_rgb = df_feat[["red", "green", "blue"]].sum(axis=1).replace(0, 1)
    df_feat["norm_r"] = df_feat["red"] / sum_rgb
    df_feat["norm_g"] = df_feat["green"] / sum_rgb
    df_feat["norm_b"] = df_feat["blue"] / sum_rgb

    radius = 3
    n_points = 8 * radius
    lbp = local_binary_pattern(gray2, n_points, radius, method="uniform")
    df_feat["lbp"] = lbp.flatten()
    log("✅ Feature extraction completed")

    X = df_feat[[
        "red","blue","green","diff_r_g","diff_r_b","diff_b_g",
        "gradient","dist_to_center","norm_r","norm_g","norm_b","lbp"
    ]]

    log("📏 Standardizing features...")
    std_scale = preprocessing.StandardScaler().fit(X)
    X_scaled = std_scale.transform(X)
    log("✅ Standardization completed")

    log("🤖 Running K-Means (k=3)...")
    kmeans = KMeans(n_clusters=3, random_state=2206, n_init=10)
    kmeans.fit(X_scaled)
    labels = kmeans.labels_
    centers = kmeans.cluster_centers_
    log("✅ K-Means segmentation completed")

    log("🗂️ Reordering clusters by 'brightness' and computing severity...")
    brightness = np.sum(centers, axis=1)
    order = np.argsort(brightness)
    reorder_map = {old: new for new, old in enumerate(order)}
    new_labels = np.vectorize(reorder_map.get)(labels)
    segmented_image = new_labels.reshape(dims[:-1])

    # Apply ROI (edge-protected mask) when computing severity
    roi_bool = roi_mask.astype(bool)
    # Convention: 0 = brightest (healthy), 2 = darkest (lesion), 1 = intermediate
    lesion_pixels = np.sum((segmented_image == 2) & roi_bool)
    healthy_pixels = np.sum((segmented_image == 0) & roi_bool)
    denom = lesion_pixels + healthy_pixels
    severity = (lesion_pixels / denom) if denom > 0 else 0.0
    log(f"✅ Severity (lesions) within ROI: {severity*100:.2f}%")

    # --- Defoliation adjustment ---
    log("🌿 Computing defoliation adjustment...")
    defoliation = max(0, missing_leaflets) / expected_leaflets if expected_leaflets > 0 else 0
    severity_multiplicative = 1 - (1 - severity) * (1 - defoliation)
    log(f"✅ Defoliation: {defoliation*100:.2f}%")
    log(f"📊 Adjusted severity (multiplicative): {severity_multiplicative*100:.2f}%")

    # --- Append to external DataFrame ---
    log("💾 Appending results to the external DataFrame...")
    results_df.loc[len(results_df)] = [
        image_route,
        n_leaflets,
        nodes,
        missing_leaflets,
        severity * 100,
        defoliation * 100,
        severity_multiplicative * 100,
    ]
    log("✅ Results saved")

    fig = None
    if show_results:
        # --- Visualization: 3 images + summary panel ---
        seg_vis = np.zeros((*segmented_image.shape, 3), dtype=np.uint8)
        seg_vis[segmented_image == 0] = (0, 255, 0)     # healthy
        seg_vis[segmented_image == 1] = (255, 255, 0)   # intermediate
        seg_vis[segmented_image == 2] = (255, 0, 0)     # lesion
        # Dim outside ROI to visualize edge protection
        seg_vis[~roi_bool] = (40, 40, 40)

        fig, axes = plt.subplots(2, 2, figsize=(16, 12))
        fig.suptitle("Leaf Lesion Severity (K-Means + Defoliation + Edge Protection)", fontsize=14, fontweight="bold")

        # 1) Original with filename
        fname = Path(image_route).name
        axes[0, 0].imshow(img_rgb_orig)
        axes[0, 0].set_title(f"Original: {fname}")
        axes[0, 0].axis("off")

        # 2) Detected leaflets
        axes[0, 1].imshow(out_rgb)
        axes[0, 1].set_title("Detected leaflets")
        axes[0, 1].axis("off")

        # 3) K-Means segmentation (with ROI dimmed)
        axes[1, 0].imshow(seg_vis)
        axes[1, 0].set_title("K-Means Segmentation (0=healthy, 1=mid, 2=lesion)\n(Outside ROI dimmed)")
        axes[1, 0].axis("off")

        # 4) Summary panel
        summary = (
            f"SUMMARY\n\n"
            f"• % Severity (lesions, ROI): {severity*100:.2f}%\n"
            f"• ROI pixels (edge-protected): {roi_area:,}\n"
            f"• Missing leaflets: {max(0, missing_leaflets)}\n"
            f"• % Defoliation: {defoliation*100:.2f}%\n"
            f"• % Adjusted severity: {severity_multiplicative*100:.2f}%\n"
        )
        axes[1, 1].axis("off")
        axes[1, 1].text(
            0.02, 0.98, summary,
            transform=axes[1, 1].transAxes,
            va="top", ha="left",
            fontsize=12, family="monospace",
            bbox=dict(boxstyle="round,pad=0.5", facecolor="lightgray", alpha=0.9)
        )

        plt.tight_layout()
        plt.show()
        log("✅ Visualization completed")

    log("🏁 Analysis done.")
    if return_figure and show_results:
        return results_df, fig
    return results_df


def save_figure(fig, output: str | os.PathLike, format: str = "png", dpi: int = 300):
    """
    Save a Matplotlib figure to PNG or PDF.

    Parameters
    ----------
    fig : matplotlib.figure.Figure
    output : str | PathLike
        Output path (with or without extension). If no extension, one is added from `format`.
    format : {"png","pdf"}
    dpi : int
    """
    format = format.lower()
    if format not in {"png", "pdf"}:
        raise ValueError("format must be 'png' or 'pdf'")
    output = Path(output)
    if output.suffix.lower() != f".{format}":
        output = output.with_suffix(f".{format}")
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, format=format, dpi=dpi, bbox_inches="tight")
    return str(output)


def save_results(df: pd.DataFrame, output: str | os.PathLike):
    """
    Save the results DataFrame to CSV, XLSX or Parquet depending on the extension.

    Parameters
    ----------
    df : pd.DataFrame
    output : str | PathLike
        Output path with extension .csv, .xlsx, or .parquet
    """
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    ext = output.suffix.lower()
    if ext == ".csv":
        df.to_csv(output, index=False)
    elif ext in {".xlsx", ".xls"}:
        df.to_excel(output, index=False)
    elif ext == ".parquet":
        df.to_parquet(output, index=False)
    else:
        raise ValueError("Unsupported extension. Use .csv, .xlsx or .parquet")
    return str(output)
