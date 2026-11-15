"""Checking a would-be cover is really an image (utils.cover_image_extension).

Run from the repository root:  python -m unittest discover tests
"""
import io
import unittest

from PIL import Image

from utils import NotACoverImageError, cover_image_extension


def image_bytes(fmt):
    out = io.BytesIO()
    Image.new('RGB', (20, 30), 'teal').save(out, fmt)
    return out.getvalue()


class CoverImageExtensionTest(unittest.TestCase):
    def test_extension_from_what_it_really_is(self):
        for fmt, ext in [('JPEG', '.jpg'), ('PNG', '.png'), ('GIF', '.gif'), ('WEBP', '.webp')]:
            self.assertEqual(cover_image_extension(image_bytes(fmt)), ext, fmt)

    def test_claimed_type_is_ignored(self):
        # A PNG served or named as a JPEG is saved as what it is
        self.assertEqual(cover_image_extension(image_bytes('PNG'), 'image/jpeg'), '.png')

    def test_a_web_page_from_a_link(self):
        page = b'\n  <!doctype html><html lang="en-au"><head><title>Frontier Summoner 3</title>'
        with self.assertRaisesRegex(NotACoverImageError, 'web page'):
            cover_image_extension(page, 'text/html; charset=utf-8', from_link=True)
        # Even when the server claims it's an image
        with self.assertRaisesRegex(NotACoverImageError, 'web page'):
            cover_image_extension(page, 'image/jpeg', from_link=True)

    def test_other_files_refused(self):
        for content in (b'', b'not an image at all', image_bytes('PNG')[:40], image_bytes('BMP')):
            with self.assertRaisesRegex(NotACoverImageError, "isn't a JPEG"):
                cover_image_extension(content)

    def test_uploaded_web_page_isnt_called_a_link(self):
        with self.assertRaisesRegex(NotACoverImageError, "isn't a JPEG"):
            cover_image_extension(b'<!doctype html><html>')


if __name__ == '__main__':
    unittest.main()
