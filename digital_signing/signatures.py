"""Validate preview signature images; no signature storage or legal acceptance here."""
import base64
import binascii
from io import BytesIO
import warnings

from PIL import Image, ImageChops, UnidentifiedImageError


def signature_error(value):
    if not isinstance(value, dict) or set(value) - {'method', 'image', 'typed_name'}:
        return 'Add your signature using Type, Draw, or Upload.'
    method = value.get('method')
    if not isinstance(method, str) or method not in {'typed', 'drawn', 'uploaded'}:
        return 'Choose Type, Draw, or Upload for your signature.'
    name = value.get('typed_name')
    if method == 'typed' and (not isinstance(name, str) or not 1 <= len(name.strip()) <= 100):
        return 'Type your name (up to 100 characters).'
    if method != 'typed' and name is not None:
        return 'Unexpected typed name for this signature method.'
    data = value.get('image')
    prefix = 'data:image/png;base64,'
    if not isinstance(data, str) or not data.startswith(prefix) or len(data) > 2_000_000:
        return 'Use a signature image smaller than 2 MB.'
    try:
        content = base64.b64decode(data[len(prefix):], validate=True)
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                if image.format != 'PNG' or image.width > 1600 or image.height > 600 or image.width < 3 or image.height < 3 or getattr(image, 'n_frames', 1) != 1:
                    return 'Use a valid, single-frame signature image up to 1600 × 600 pixels.'
                image.load()
                rgba = image.convert('RGBA')
                white = Image.new('RGBA', rgba.size, 'white')
                white.alpha_composite(rgba)
                rgb = white.convert('RGB')
                # Ignore transparent/near-white backgrounds, accepting colored pen strokes.
                channels = rgb.split()
                dark = ImageChops.darker(ImageChops.darker(channels[0], channels[1]), channels[2])
                mask = dark.point(lambda pixel: 255 if pixel < 240 else 0)
                bbox = mask.getbbox()
                if not bbox or bbox[2]-bbox[0] < 3 or bbox[3]-bbox[1] < 3 or mask.histogram()[255] < 12:
                    return 'The signature image is blank. Add a visible signature.'
    except (ValueError, TypeError, binascii.Error, UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return 'This signature image could not be read. Try drawing, typing, or uploading it again.'
    return None
