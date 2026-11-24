from dataclasses import dataclass
from math import atan2, cos, degrees, hypot, radians, sin

IDENTITY = (1., 0., 0., 1., 0., 0.)


def compose(outer, inner):
    a, b, c, d, e, f = outer
    u, v, w, x, y, z = inner
    return a*u+c*v, b*u+d*v, a*w+c*x, b*w+d*x, a*y+c*z+e, b*y+d*z+f


def point(matrix, x, y):
    a, b, c, d, e, f = matrix
    return a*x+c*y+e, b*x+d*y+f


def inverse(matrix):
    a, b, c, d, e, f = matrix
    det = a*d-b*c
    return d/det, -b/det, -c/det, a/det, (c*f-d*e)/det, (b*e-a*f)/det


def box(matrix, bounds):
    l, b, r, t = bounds
    points = [point(matrix, x, y) for x in (l, r) for y in (b, t)]
    xs, ys = zip(*points)
    return min(xs), min(ys), max(xs), max(ys)


def union(boxes):
    boxes = tuple(boxes)
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def translate_angle(x, y, angle, scale=1., shear=0.):
    c, s = cos(radians(angle))*scale, sin(radians(angle))*scale
    return c, s, c*shear-s, s*shear+c, x, y


def similarity(matrix):
    a, b, c, d, _, _ = matrix
    size = hypot(a, b)
    if size < 1e-8 or abs(a-d) > 0.002*size or abs(b+c) > 0.002*size:
        return None
    return size, degrees(atan2(b, a))


def text_transform(matrix):
    """Decompose a uniformly sized text matrix with an optional baseline shear."""
    a, b, c, d, _, _ = matrix
    size = hypot(a, b)
    if size < 1e-8:
        return None
    norm = size*size
    shear = (a*c+b*d)/norm
    height = (a*d-b*c)/norm
    if abs(height-1.) > .002 or abs(shear) > .5:
        return None
    return size, degrees(atan2(b, a)), shear


@dataclass(frozen=True)
class Sheet:
    width: float
    height: float
    crop: tuple
    rotation: int

    @property
    def to_screen(self):
        l, b, r, t = self.crop
        return {
            0: (1, 0, 0, -1, -l, t),
            90: (0, 1, 1, 0, -b, -l),
            180: (-1, 0, 0, 1, r, -b),
            270: (0, -1, -1, 0, t, r),
        }[self.rotation]
