"""Fan curve evaluation.

A curve is a list of ``[temperature_c, fan_percent]`` points. Between points
the output is linearly interpolated; outside the range it is clamped. A
one-directional hysteresis band stops the fan oscillating when the temperature
sits right on a boundary, which is the usual failure mode of naive fan curves.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class CurveError(ValueError):
    pass


def validate(points: list) -> list[tuple[float, float]]:
    """Normalise and sanity-check curve points.

    Rejects curves that would be unsafe or nonsensical rather than silently
    clamping them, because a typo in a config file should be loud.
    """
    if not points:
        raise CurveError("fan curve is empty")
    cleaned: list[tuple[float, float]] = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise CurveError("each curve point must be [temp_c, percent], got %r" % (point,))
        try:
            temp, percent = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            raise CurveError("non-numeric curve point %r" % (point,)) from None
        if not -20 <= temp <= 120:
            raise CurveError("curve temperature %g C is out of range" % temp)
        if not 0 <= percent <= 100:
            raise CurveError("curve percent %g is out of range 0-100" % percent)
        cleaned.append((temp, percent))

    cleaned.sort(key=lambda p: p[0])
    temps = [p[0] for p in cleaned]
    if len(set(temps)) != len(temps):
        raise CurveError("curve has duplicate temperatures: %s" % temps)
    percents = [p[1] for p in cleaned]
    if any(b < a for a, b in zip(percents, percents[1:])):
        raise CurveError(
            "curve percentages must not decrease as temperature rises: %s" % percents
        )
    return cleaned


def evaluate(points: list[tuple[float, float]], temp: float) -> float:
    """Interpolate the curve at ``temp``, clamping outside its range."""
    if temp <= points[0][0]:
        return points[0][1]
    if temp >= points[-1][0]:
        return points[-1][1]
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        if t0 <= temp <= t1:
            if t1 == t0:
                return p1
            return p0 + (p1 - p0) * (temp - t0) / (t1 - t0)
    return points[-1][1]


class CurveController:
    """Stateful curve evaluator with downward hysteresis.

    The fan follows the curve immediately when temperature rises, but only
    steps down once the temperature has fallen ``hysteresis_c`` below the
    point that justified the current speed. This gives quiet, stable behaviour
    without lagging behind a real thermal load.
    """

    def __init__(self, points: list, hysteresis_c: float = 3.0):
        self.points = validate(points)
        self.hysteresis_c = max(0.0, float(hysteresis_c))
        self._current: float | None = None
        self._peak_temp: float | None = None

    def reset(self) -> None:
        self._current = None
        self._peak_temp = None

    def update(self, temp: float) -> float:
        """Return the fan percentage to apply for the observed temperature."""
        target = evaluate(self.points, temp)

        if self._current is None:
            self._current, self._peak_temp = target, temp
            return target

        if target >= self._current:
            # Rising: respond at once.
            self._current, self._peak_temp = target, temp
            return target

        # Falling: hold until we are clear of the hysteresis band.
        if self._peak_temp is not None and temp > self._peak_temp - self.hysteresis_c:
            return self._current

        self._current, self._peak_temp = target, temp
        return target
