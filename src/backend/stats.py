import time
from pdf2image import convert_from_path
from pathlib import Path
import numpy as np
import cv2

BASE_DIR = Path(__file__).resolve()
UPLOAD_DIR = BASE_DIR / "uploads"
# UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_RESOLUTION = 9497600

EXTENSIONS_IMAGES = [".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"]

dpi_value = 300

def lighting_uniformity(gray):
    blur = cv2.GaussianBlur(gray, (51, 51), 0)

    diff = cv2.absdiff(gray, blur)

    return float(1 - (np.mean(diff) / 255))

def entropy(gray):
    # histogramme des intensités
    hist = cv2.calcHist([gray], [0], None, [256], [0, 256])
    
    # normalisation en probabilité
    hist = hist / hist.sum()

    # éviter log(0)
    hist = hist[hist > 0]

    # entropie de Shannon
    return float(-np.sum(hist * np.log2(hist)))

def black_pixel_ratio(gray):
    # seuil pour considérer un pixel comme "noir"
    threshold = 50

    black_pixels = np.sum(gray < threshold)
    total_pixels = gray.size

    return black_pixels / total_pixels

def is_image(file: Path):
    return file.suffix.lower() in EXTENSIONS_IMAGES

def get_image_extension(file: Path):
    return file.suffix.upper()

def is_pdf(file: Path):
    return file.suffix.lower() == ".pdf"

def detect_skew(image):
    """
    Détection robuste de l'inclinaison d'un document texte.
    
    Retour:
        angle (float)
    """

    # =========================
    # Conversion gris
    # =========================
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    # =========================
    # Réduction du bruit
    # =========================
    gray = cv2.GaussianBlur(gray, (5, 5), 0)

    # =========================
    # Binarisation
    # =========================
    thresh = cv2.threshold(
        gray,
        0,
        255,
        cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )[1]

    # =========================
    # Fusion des caractères
    # pour créer des lignes de texte
    # =========================
    kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (30, 5)
    )

    dilated = cv2.dilate(
        thresh,
        kernel,
        iterations=1
    )

    # =========================
    # Recherche contours
    # =========================
    contours, _ = cv2.findContours(
        dilated,
        cv2.RETR_LIST,
        cv2.CHAIN_APPROX_SIMPLE
    )

    angles = []

    for contour in contours:

        area = cv2.contourArea(contour)

        # ignorer petits bruits
        if area < 500:
            continue

        rect = cv2.minAreaRect(contour)

        angle = rect[-1]

        # normalisation angle
        if angle < -45:
            angle = 90 + angle

        # filtrage angles absurdes
        if -45 <= angle <= 45:
            angles.append(angle)

    if len(angles) == 0:
        return 0.0

    # =========================
    # Médiane robuste
    # =========================
    final_angle = float(np.median(angles))

    return round(final_angle, 2)

# =========================
# IMAGE STATS
# =========================

def image_stats(file: Path):
    print(file.name)
    debut = time.perf_counter()
    image_opencv = cv2.imdecode(
        np.fromfile(file, dtype=np.uint8),
        cv2.IMREAD_COLOR
    )

    if image_opencv is None:
        return {
            "filename": file.name,
            "error": "Impossible de lire l'image"
        }

    hauteur, largeur = image_opencv.shape[:2]

    gris = cv2.cvtColor(image_opencv, cv2.COLOR_BGR2GRAY)

    blur = float(
        cv2.Laplacian(gris, cv2.CV_64F).var()
    )

    #resize avec le coefficient
    # coef = round(MAX_RESOLUTION / (largeur * hauteur),6)
    coef = round(np.sqrt(MAX_RESOLUTION / (largeur * hauteur)),12)
    nouvelle_largeur = round(largeur * coef)
    nouvelle_hauteur = round(hauteur * coef)
    image_resize = cv2.resize(
        image_opencv,
        (nouvelle_largeur, nouvelle_hauteur),
        interpolation=cv2.INTER_AREA
    )
    gris_new = cv2.cvtColor(image_resize, cv2.COLOR_BGR2GRAY)
    blur_new = float(
        cv2.Laplacian(gris_new, cv2.CV_64F).var()
    )

    brightness = round(
        float(np.mean(gris)) / 255 * 100,
        1
    )

    contrast = round(
        float(np.std(gris)),
        1
    )

    skew = detect_skew(image_opencv)
    
    blurred = cv2.GaussianBlur(gris,(5,5),0)
    noise_score = float(np.std(gris - blurred))
    
    taille_mb = round(
        file.stat().st_size / (1024 * 1024),
        2
    )
    img_entropy = entropy(gris)
    lighting = lighting_uniformity(gris)
    black_ratio = float(black_pixel_ratio(gris))
    temps = round(
        time.perf_counter() - debut,
        3
    )
    return {
                "page": 1,
                "dpi":dpi_value,
                "blur": blur,
                "blur_new":blur_new,
                "largeur": largeur,
                "hauteur": hauteur,
                "contrast": contrast,
                "brightness": brightness,
                "skew": skew,
                "noise_score":round(noise_score,3),
                "black_pixel_ratio":round(black_ratio,3),
                "entropy":round(img_entropy,3),
                "lighting_uniformity":round(lighting,3),
                "time": temps,
            }

# =========================
# PDF STATS
# =========================
def pdf_stats(file: Path):
    # info = pdfinfo_from_path(file)

    pages = convert_from_path(
        file,
        dpi=dpi_value
    )

    pages_data = []

    for index, page in enumerate(pages):
        debut = time.perf_counter()
        image = np.array(page)
        image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        gris = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        hauteur, largeur = gris.shape
        blur = float(cv2.Laplacian(gris, cv2.CV_64F).var())
        coef = round(np.sqrt(MAX_RESOLUTION / (largeur * hauteur)), 12)
        nouvelle_largeur = round(largeur * coef)
        nouvelle_hauteur = round(hauteur * coef)
        image_resize = cv2.resize(
            image,
            (nouvelle_largeur, nouvelle_hauteur),
            interpolation=cv2.INTER_AREA
        )
        gris_new = cv2.cvtColor(image_resize, cv2.COLOR_BGR2GRAY)
        blur_new = float(
            cv2.Laplacian(gris_new, cv2.CV_64F).var()
        )
        brightness = round(float(np.mean(gris)) / 255 * 100, 1)
        contrast = round(float(np.std(gris)), 1)

        skew = detect_skew(image)
        blurred = cv2.GaussianBlur(gris,(5,5),0)
        noise_score = float(np.std(gris - blurred))

        img_entropy = entropy(gris)
        lighting = lighting_uniformity(gris)
        black_ratio = float(black_pixel_ratio(gris))
        temps = round(
            time.perf_counter() - debut,
            3
        )
        pages_data.append({
            "page": index + 1,
            "dpi":dpi_value,
            "blur": blur,
            "blur_new":blur_new,
            "largeur": largeur,
            "hauteur": hauteur,
            "contrast": contrast,
            "brightness": brightness,
            "skew": skew,
            "noise_score":round(noise_score,3),
            "black_pixel_ratio":round(black_ratio,3),
            "entropy":round(img_entropy,3),
            "lighting_uniformity":round(lighting,3),
            "time": temps,
        })

    return pages_data