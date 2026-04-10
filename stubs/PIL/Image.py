"""PIL.Image 스텁 — pptx 임포트 통과용"""


class Image:
    pass


def open(*args, **kwargs):
    raise NotImplementedError("PIL stub: Image.open not available")
